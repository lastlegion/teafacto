from teafacto.blocks.basic import MatDot as Lin, Softmax, VectorEmbed
from teafacto.blocks.attention import Attention, LinearGateAttentionGenerator, WeightedSumAttCon
from teafacto.blocks.basic import VectorEmbed, IdxToOneHot, MatDot
from teafacto.blocks.rnn import RecStack, SeqDecoder, BiRNU, SeqEncoder
from teafacto.blocks.rnu import GRU
from teafacto.core.base import Block, tensorops as T
from teafacto.util import issequence


class SeqEncDec(Block):
    def __init__(self, enc, dec, statetrans=None, **kw):
        super(SeqEncDec, self).__init__(**kw)
        self.enc = enc
        self.dec = dec
        if isinstance(statetrans, Block):
            self.statetrans = lambda x, y: statetrans(x)
        elif statetrans is True:
            self.statetrans = lambda x, y: x
        else:
            self.statetrans = statetrans

    def apply(self, inpseq, outseq, maskseq=None):
        if maskseq is None:
            mask = "auto"
        else:
            mask = maskseq
        enco, allenco = self.enc(inpseq, mask=mask)
        mask = None
        if self.statetrans is not None:
            topstate = self.statetrans(enco, allenco)
            deco = self.dec(allenco, outseq, initstates=[topstate], mask=mask)
        else:
            deco = self.dec(allenco, outseq, mask=mask)      # no state transfer
        return deco

    def get_init_info(self, inpseq, batsize, maskseq=None):
        enco, allenco = self.enc(inpseq, mask=maskseq)
        if self.statetrans is not None:
            topstate = self.statetrans(enco, allenco)
            initstates = [topstate]
        else:
            initstates = batsize
        return self.dec.get_init_info(allenco, None, initstates)

    def rec(self, x_t, *states):
        return self.dec.rec(x_t, *states)


class SeqEncDecAtt(SeqEncDec):
    def __init__(self, enclayers, declayers, attgen, attcon, decinnerdim, inconcat, outconcat, statetrans=None, **kw):
        enc = SeqEncoder(*enclayers).with_outputs.zeromask
        dec = SeqDecoder(
            declayers,
            attention=Attention(attgen, attcon),
            innerdim=decinnerdim,
            outconcat=outconcat,
            inconcat=inconcat
        )
        super(SeqEncDecAtt, self).__init__(enc, dec, statetrans=statetrans, **kw)


class SimpleSeqEncDecAtt(SeqEncDecAtt):
    def __init__(self,
                 inpvocsize=400,
                 inpembdim=None,
                 outvocsize=100,
                 outembdim=None,
                 encdim=100,
                 decdim=100,
                 attdim=100,
                 bidir=False,
                 rnu=GRU,
                 outconcat=True,
                 inconcat=False,
                 statetrans=None,
                 **kw):
        encinnerdim = [encdim] if not issequence(encdim) else encdim
        decinnerdim = [decdim] if not issequence(decdim) else decdim

        # encoder stack
        if inpembdim is None:
            inpemb = IdxToOneHot(inpvocsize)
            inpembdim = inpvocsize
        else:
            inpemb = VectorEmbed(indim=inpvocsize, dim=inpembdim)
        encrnus = []
        dims = [inpembdim] + encinnerdim
        i = 1
        lastencinnerdim = dims[-1] if not bidir else dims[-1]*2
        while i < len(dims):
            if bidir:
                newrnu = BiRNU.fromrnu(rnu, dim=dims[i-1], innerdim=dims[i])
            else:
                newrnu = rnu(dim=dims[i-1], innerdim=dims[i])
            encrnus.append(newrnu)
            i += 1
        enclayers = [inpemb] + encrnus

        # attention
        lastdecinnerdim = decinnerdim[-1]
        attgen = LinearGateAttentionGenerator(indim=lastencinnerdim + lastdecinnerdim, attdim=attdim)
        attcon = WeightedSumAttCon()

        # decoder
        if outembdim is None:
            outemb = IdxToOneHot(outvocsize)
            outembdim = outvocsize
        else:
            outemb = VectorEmbed(indim=outvocsize, dim=outembdim)
        decrnus = []
        firstdecdim = outembdim if inconcat is False else outembdim + encinnerdim
        dims = [firstdecdim] + decinnerdim
        i = 1
        while i < len(dims):
            decrnus.append(rnu(dim=dims[i-1], innerdim=dims[i]))
            i += 1
        declayers = [outemb] + decrnus
        argdecinnerdim = lastdecinnerdim if outconcat is False else lastencinnerdim + lastdecinnerdim

        if statetrans is True:
            if lastencinnerdim != lastdecinnerdim:  # state shape mismatch
                statetrans = MatDot(lastencinnerdim, lastdecinnerdim)

        super(SimpleSeqEncDecAtt, self).__init__(enclayers, declayers, attgen, attcon, argdecinnerdim, inconcat, outconcat, statetrans=statetrans, **kw)


class SeqTransducer(Block):
    def __init__(self, embedder, *layers, **kw):
        """ layers must have an embedding layers first, final softmax layer is added automatically"""
        assert("smodim" in kw and "outdim" in kw)
        self.embedder = embedder
        smodim = kw["smodim"]
        outdim = kw["outdim"]
        del kw["smodim"]; del kw["outdim"]
        super(SeqTransducer, self).__init__(**kw)
        self.block = RecStack(*(layers + (Lin(indim=smodim, dim=outdim), Softmax())))

    def apply(self, inpseq, maskseq=None):    # inpseq: idx^(batsize, seqlen), maskseq: f32^(batsize, seqlen)
        embseq = self.embedder(inpseq)
        res = self.block(embseq, mask=maskseq)            # f32^(batsize, seqlen, outdim)
        ret = self.applymask(res, maskseq=maskseq)
        return ret

    @classmethod
    def applymask(cls, xseq, maskseq=None):
        if maskseq is None:
            ret = xseq
        else:
            mask = T.tensordot(maskseq, T.ones((xseq.shape[2],)), 0)  # f32^(batsize, seqlen, outdim) -- maskseq stacked
            masker = T.concatenate([T.ones((xseq.shape[0], xseq.shape[1], 1)), T.zeros((xseq.shape[0], xseq.shape[1], xseq.shape[2] - 1))], axis=2)  # f32^(batsize, seqlen, outdim) -- gives 100% prob to output 0
            ret = xseq * mask + masker * (1.0 - mask)
        return ret


class SimpleSeqTransducer(SeqTransducer):
    def __init__(self, indim=400, embdim=50, innerdim=100, outdim=50, **kw):
        self.emb = VectorEmbed(indim=indim, dim=embdim)
        if not issequence(innerdim):
            innerdim = [innerdim]
        innerdim = [embdim] + innerdim
        self.rnn = self.getrnnfrominnerdim(innerdim)
        super(SimpleSeqTransducer, self).__init__(self.emb, *self.rnn, smodim=innerdim[-1], outdim=outdim, **kw)

    @classmethod
    def getrnnfrominnerdim(self, innerdim, rnu=GRU):
        rnn = []
        assert(len(innerdim) >= 2)
        i = 1
        while i < len(innerdim):
            rnn.append(rnu(dim=innerdim[i-1], innerdim=innerdim[i]))
            i += 1
        return rnn


class SeqTransDec(Block):
    def __init__(self, *layers, **kw):
        """ first two layers must be embedding layers. Final softmax is added automatically"""
        assert("smodim" in kw and "outdim" in kw)
        smodim = kw["smodim"]
        outdim = kw["outdim"]
        del kw["smodim"]; del kw["outdim"]
        super(SeqTransDec, self).__init__(**kw)
        self.inpemb = layers[0]
        self.outemb = layers[1]
        self.block = RecStack(*(layers[2:] + (Lin(indim=smodim, dim=outdim), Softmax())))

    def apply(self, inpseq, outseq, maskseq=None):
        # embed with the two embedding layers
        emb = self._get_emb(inpseq, outseq)
        res = self.block(emb)
        ret = SeqTransducer.applymask(res, maskseq=maskseq)
        return ret

    def _get_emb(self, inpseq, outseq):
        iemb = self.inpemb(inpseq)     # (batsize, seqlen, inpembdim)
        oemb = self.outemb(outseq)     # (batsize, seqlen, outembdim)
        emb = T.concatenate([iemb, oemb], axis=iemb.ndim-1)                       # (batsize, seqlen, inpembdim+outembdim)
        return emb

    def rec(self, inpa, inpb, *states):
        emb = self._get_emb(inpa, inpb)
        return self.block.rec(emb, *states)

    def get_init_info(self, initstates):
        return self.block.get_init_info(initstates)


class SimpleSeqTransDec(SeqTransDec):
    def __init__(self, indim=400, outdim=50, inpembdim=50, outembdim=50, innerdim=100, **kw):
        self.inpemb = VectorEmbed(indim=indim, dim=inpembdim)
        self.outemb = VectorEmbed(indim=outdim, dim=outembdim)
        self.rnn = []
        if not issequence(innerdim):
            innerdim = [innerdim]
        innerdim = [inpembdim+outembdim] + innerdim
        self.rnn = SimpleSeqTransducer.getrnnfrominnerdim(innerdim)
        super(SimpleSeqTransDec, self).__init__(self.inpemb, self.outemb, *self.rnn, smodim=innerdim[-1], outdim=outdim, **kw)