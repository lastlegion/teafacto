from teafacto.blocks.rnu import GRU, ReccableBlock, RecurrentBlock, RNUBase
from teafacto.core.base import Block, tensorops as T, asblock
from teafacto.blocks.basic import IdxToOneHot, VectorEmbed, Embedder, Softmax, MatDot
from teafacto.blocks.attention import WeightedSum, LinearSumAttentionGenerator, Attention, AttentionConsumer, LinearGateAttentionGenerator
from teafacto.util import issequence
import inspect
from IPython import embed


class ReccableStack(ReccableBlock):
    def __init__(self, *args, **kw): # layer can be a layer or function
        super(ReccableStack, self).__init__(**kw)
        self.layers = args

    def __getitem__(self, idx):
        return self.layers[idx]

    def get_states_from_outputs(self, outputs):
        # outputs are ordered from topmost recurrent layer first ==> split and delegate
        states = []
        for recurrentlayer in filter(lambda x: isinstance(x, ReccableBlock), self.layers): # from bottom -> eat from behind; insert to the front
            numstates = len(inspect.getargspec(recurrentlayer.rec).args) - 2
            layerstates = recurrentlayer.get_states_from_outputs(outputs[-numstates:]) # might be more than one
            i = 0
            for layerstate in layerstates:
                states.insert(i, layerstate)
                i += 1
            outputs = outputs[:-numstates]
        assert(len(outputs) == 0)
        return states

    def do_get_init_info(self, initstates):    # if initstates is not a list, it must be batsize
        recurrentlayers = list(filter(lambda x: isinstance(x, ReccableBlock), self.layers))
        recurrentlayers.reverse()
        init_infos = []
        for recurrentlayer in recurrentlayers:
            initinfo, initstates = recurrentlayer.do_get_init_info(initstates)
            init_infos.extend(initinfo)
        return init_infos, initstates   # layerwise in reverse

    def rec(self, x_t, *states):
        # apply each block on x_t to get next-level input, consume states in the process
        nextinp = x_t
        nextstates = []
        for block in self.layers:
            if isinstance(block, ReccableBlock):
                numstates = len(inspect.getargspec(block.rec).args) - 2
                # eat from behind
                recstates = states[-numstates:]
                states = states[:-numstates]
                rnuret = block.rec(nextinp, *recstates)
                # insert from behind
                i = 0
                for nextstate in rnuret[1:]:
                    nextstates.insert(i, nextstate)
                    i += 1
                nextinp = rnuret[0]
            elif isinstance(block, Block): # block is a function
                nextinp = block(nextinp)
        return [nextinp] + nextstates

    def apply(self, seq, initstates=None):
        seq = seq.dimswap(1, 0)
        initstatearg = initstates if initstates is not None else seq.shape[1]
        outputs, _ = T.scan(fn=self.rec,
                            sequences=seq,
                            outputs_info=[None]+self.get_init_info(initstatearg))
        output = outputs[0]
        return output.dimswap(1, 0)


class RecurrentStack(Block):       # TODO: setting init states of contained recurrent blocks
    def __init__(self, *layers, **kw):
        super(RecurrentStack, self).__init__(**kw)
        self.layers = layers

    def apply(self, seq):
        acc = seq
        for layer in self.layers:
            if isinstance(layer, RecurrentBlock):
                acc = layer(acc)
            elif isinstance(layer, Block): # non-recurrent ==> recur
                acc = acc.dimswap(1, 0)
                acc, _ = T.scan(fn=self.dummyrec(layer),
                                  sequences=acc,
                                  outputs_info=None)
                acc = acc.dimswap(1, 0)
            else:
                raise Exception("can not apply this layer: "+str(layer))
        return acc

    def dummyrec(self, layer):
        def innerrec(x_t):
            return layer(x_t)
        return innerrec




class ReccableBlockParameterized(object):
    def __init__(self, *layers, **kw):
        self._reverse = False
        if "reverse" in kw:
            self._reverse = kw["reverse"]
            del kw["reverse"]
        super(ReccableBlockParameterized, self).__init__(**kw)
        if len(layers) > 0:
            if len(layers) == 1:
                self.block = layers[0]
                assert(isinstance(self.block, ReccableBlock))
            else:
                self.block = ReccableStack(*layers)
        else:
            self.block = None


'''class AttentionParameterized(object):
    def __add__(self, other):
        assert(self.block is None)  # this block should not be parameterized already in order to parameterize it
        self.attach(other)
        return self

    def attach(self, other):
        if isinstance(other, RecurrentBlock):
            self.block = other
            self.onAttach()
        return self

    def onAttach(self):
        raise NotImplementedError("use subclass")
'''

class BiRNU(RecurrentBlock): # TODO: optimizer can't process this
    def __init__(self, fwd=None, rew=None, **kw):
        super(BiRNU, self).__init__(**kw)
        assert(isinstance(fwd, RNUBase) and isinstance(rew, RNUBase))
        self.fwd = fwd
        self.rew = rew
        assert(self.fwd.indim == self.rew.indim)

    @classmethod
    def fromrnu(cls, rnucls, *args, **kw):
        assert(issubclass(rnucls, RNUBase))
        kw["reverse"] = False
        fwd = rnucls(*args, **kw)
        kw["reverse"] = True
        rew = rnucls(*args, **kw)
        return cls(fwd, rew)

    def apply(self, seq, init_states=None):
        fwdout = self.fwd(seq)
        rewout = self.rew(seq)
        # concatenate: fwdout, rewout: (batsize, seqlen, feats) ==> (batsize, seqlen, feats_fwd+feats_rew)
        out = T.concatenate([fwdout, rewout], axis=2)
        return out




class SeqEncoder(ReccableBlockParameterized, AttentionConsumer, Block):
    '''
    Encodes a sequence of vectors into a vector, input dims and output dims specified by the RNU unit
    Returns multiple outputs, multiple states
    Builds for one output
    '''
    _withoutput = False
    _all_states = False
    _weighted = False
    _nomask = False

    @property
    def nomask(self):
        self._nomask = True

    def apply(self, seq, weights=None): # seq: (batsize, seqlen, dim), weights: (batsize, seqlen)
        inp = seq.dimswap(1, 0)         # inp: (seqlen, batsize, dim)
        if weights is None:
            weights = T.ones((inp.shape[0], inp.shape[1])) # (seqlen, batsize)
        else:
            self._weighted = True
            weights = weights.dimswap(1, 0)
        outputs, _ = T.scan(fn=self.recwrap,
                            sequences=[inp, weights],
                            outputs_info=[None]+self.block.get_init_info(seq.shape[0]),
                            go_backwards=self._reverse)
        return self._get_apply_outputs(outputs)

    def _get_apply_outputs(self, outputs):
        output = outputs[0]
        if self._all_states:    # get the last values of each of the states
            states = self.block.get_states_from_outputs(outputs[1:])
            ret = [s[-1, :, :] for s in states]
        else:                   # get the last state of the final state
            ret = [output[-1, :, :]] #output is (batsize, innerdim)
        if self._withoutput:    # include stack output
            return ret + [output]
        else:
            if len(ret) == 1:   # if only topmost last state or only one stateful layer --> one output
                return ret[0]
            else:               # else: a list of outputs
                return ret

    def _build(self, *inps):
        res = self.wrapply(*inps)
        if issequence(res):
            output = res[0]
        else:
            output = res
        return output

    def recwrap(self, x_t, w_t, *args): # x_t: (batsize, dim)      if input is all zeros, just return previous state
        if not self._nomask:
            if x_t.ndim == 1:       # ==> indexes
                mask = x_t > 0      # 0 is TERMINUS
            else:
                mask = x_t.norm(2, axis=1) > 0 # mask: (batsize, )
        rnuret = self.block.rec(x_t, *args) # list of matrices (batsize, **somedims**)
        if self._weighted:
            rnuret = map(lambda (origarg, rnuretarg): (origarg.T * (1 - w_t) + rnuretarg.T * w_t).T, zip([args[0]] + list(args), rnuret))
        if not self._nomask:
            ret = map(lambda (origarg, rnuretarg): (origarg.T * (1 - mask) + rnuretarg.T * mask).T, zip([args[0]] + list(args), rnuret)) # TODO mask breaks multi-layered encoders (order is reversed)
        else:
            ret = rnuret
        #ret = rnuret
        return ret

    @property
    def all_states(self):
        '''Call this switch to get the final states of all recurrent layers'''
        self._all_states = True
        return self

    @property
    def with_outputs(self):
        '''Call this switch to get the actual output of top layer as the last outputs'''
        self._withoutput = True
        return self


class SeqDecoder(ReccableBlockParameterized, Block):
    '''
    Decodes a sequence of symbols given context
    output: probabilities over symbol space: float: (batsize, seqlen, vocabsize)

    ! must pass in a recurrent block that takes two arguments: context_t and x_t
    ! first input is TERMINUS ==> suggest to set TERMINUS(0) embedding to all zeroes (in s2vf)
    ! first layer must be an embedder or IdxToOneHot, otherwise, an IdxToOneHot is created automatically based on given dim
    '''
    def __init__(self, embedder, contextrecurrentblock, softmaxoutblock=None, **kw): # limit says at most how many is produced
        super(SeqDecoder, self).__init__(contextrecurrentblock, **kw)
        self._mask = False
        self._attention = None
        self.embedder = embedder
        assert(isinstance(contextrecurrentblock, ContextReccableBlock))
        if softmaxoutblock is None:
            sm = Softmax()
            lin = MatDot(indim=contextrecurrentblock.outdim, dim=self.embedder.indim)
            self.softmaxoutblock = asblock(lambda x: sm(lin(x)))
        else:
            self.softmaxoutblock = softmaxoutblock

    def apply(self, context, seq, **kw):    # context: (batsize, enc.innerdim), seq: idxs-(batsize, seqlen)
        sequences = [seq.dimswap(1, 0)]     # sequences: (seqlen, batsize)
        init_info = self.block.get_init_info(context)   # here is where the recurrent payload block decides how to use provided context (as init states or otherwise)
        '''if init_states == None:
            init_info = self.block.get_init_info(seq.shape[0])      # initial rec states initted to zeros
        else:
            init_info = init_states                                 # initial rec states initted to given values'''
        outputs, _ = T.scan(fn=self.recwrap,
                            sequences=sequences,
                            outputs_info=[None, context, 0]+init_info)
        return outputs[0].dimswap(1, 0) # returns probabilities of symbols --> (batsize, seqlen, vocabsize)

    def recwrap(self, x_t, context, t, *states_tm1):    # x_t: (batsize), context: (batsize, enc.innerdim)
        i_t = self.embedder(x_t)                    # i_t: (batsize, embdim)
        rnuret = self.block.rec(i_t, context, *states_tm1)     # list of matrices (batsize, **somedims**)
        ret = rnuret
        t = t + 1
        h_t = ret[0]
        states_t = ret[1:]
        y_t = self.softmaxoutblock(h_t)
        return [y_t, context, t] + states_t #, {}, T.until( (i > 1) * T.eq(mask.norm(1), 0) )


class ContextReccableBlock(ReccableBlock): # responsible for using context and sequence (embedded already)
    def __init__(self, outdim=50, **kw):
        super(ContextReccableBlock, self).__init__(**kw)
        self.outdim = outdim

    def rec(self, x_t, context, *args):
        raise NotImplementedError("use subclass")


class RecParamCRex(ReccableBlockParameterized, ContextReccableBlock):
    def get_states_from_outputs(self, outputs):
        return self.block.get_states_from_outputs(outputs)


class AttRecParamCRex(RecParamCRex):    # if you use attention, provide it as first argument to constructor
    def __init__(self, *layers, **kw):
        self.attention = None
        if isinstance(layers[0], Attention):
            self.attention = layers[0]
            layers = layers[1:]
        super(RecParamCRex, self).__init__(*layers, **kw)

    def _gen_context(self, multicontext, *states):
        if self.attention is not None:
            criterion = T.concatenate(self.block.get_states_from_outputs(states), axis=1)   # states are (batsize, statedim)
            return self.attention(criterion, multicontext)   # ==> criterion is (batsize, sum_of_statedim), context is (batsize, ...)
        else:
            return multicontext


class InConcatCRex(AttRecParamCRex):
    def rec(self, x_t, context, *states_tm1):
        context_t = self._gen_context(context, *states_tm1)
        i_t = T.concatenate([x_t, context_t], axis=1)
        rnuret = self.block.rec(i_t, *states_tm1)
        return rnuret

    def do_get_init_info(self, initstates):
        return self.block.do_get_init_info(initstates.shape[0])


class OutConcatCRex(AttRecParamCRex):
    def rec(self, x_t, context, *states_tm1):   # context: (batsize, context_dim), x_t: (batsize, embdim)
        rnuret = self.block.rec(x_t, *states_tm1)
        h_t = rnuret[0]                         # h_t: (batsize, rnu.innerdim)
        states_t = rnuret[1:]
        context_t = self._gen_context(context, *states_t)
        o_t = T.concatenate([h_t, context_t], axis=1) # o_t: (batsize, context_dim + block.outdim)
        return [o_t] + states_t

    def do_get_init_info(self, initstates):
        return self.block.do_get_init_info(initstates.shape[0])


class StateSetCRex(RecParamCRex):
    def rec(self, x_t, context, *states_tm1):
        rnuret = self.block.rec(x_t, *states_tm1)
        return rnuret

    def do_get_init_info(self, initstates):
        return self.block.do_get_init_info([initstates])


class SeqEncoderDecoder(Block):
    def __init__(self, inpemb, encrec, outemb, decrec, **kw):
        super(SeqEncoderDecoder, self).__init__(**kw)
        self.enc = SeqEncoder(inpemb, encrec)
        self.dec = SeqDecoder(outemb, decrec)

    def apply(self, inpseq, outseq):
        enco = self.enc(inpseq)         # (batsize, encrec.innerdim)
        deco = self.dec(enco, outseq)   # (batsize, seqlen, outvocsize)
        return deco


# TODO: travis error messages about theano optimization and shapes only involve things below
class SimpleEncoderDecoder(SeqEncoderDecoder):  # gets two sequences of indexes for training
    def __init__(self, innerdim=50, input_vocsize=100, output_vocsize=100, **kw):
        input_embedder = IdxToOneHot(input_vocsize)
        output_embedder = IdxToOneHot(output_vocsize)
        encrec = GRU(dim=input_vocsize, innerdim=innerdim)
        decrecrnu = GRU(dim=output_vocsize, innerdim=innerdim)
        decrec = OutConcatCRex(decrecrnu, outdim=innerdim+innerdim)
        super(SimpleEncoderDecoder, self).__init__(input_embedder, encrec, output_embedder, decrec, **kw)


class RNNAutoEncoder(Block):    # tries to decode original sequence
    def __init__(self, vocsize=25, encdim=200, innerdim=200, seqlen=50, **kw):
        super(RNNAutoEncoder, self).__init__(**kw)
        self.seqlen = seqlen
        self.encoder = SeqEncoder(
            IdxToOneHot(vocsize=vocsize),
            GRU(dim=vocsize, innerdim=encdim))
        self.decoder = SeqDecoder(IdxToOneHot(vocsize),
                                  InConcatCRex(GRU(dim=vocsize+encdim, innerdim=innerdim), outdim=innerdim))

    def apply(self, inpseq, outseq):    # inpseq: (batsize, seqlen), indexes
        enc = self.encoder(inpseq)
        dec = self.decoder(enc, outseq)
        return dec



class RewAttRNNEncDecoder(Block):
    '''
    Take the input index sequence as-is, transform to one-hot, feed to gate AttentionGenerator, encode with weighted SeqEncoder,
    put everything as attention inside SeqDecoder
    '''
    def __init__(self, vocsize=25, outvocsize=20, encdim=200, innerdim=200, attdim=50, **kw):
        super(RewAttRNNEncDecoder, self).__init__(**kw)
        self.emb = IdxToOneHot(vocsize)
        attgen = LinearGateAttentionGenerator(indim=innerdim+vocsize, innerdim=attdim)
        attcon = SeqEncoder(
            GRU(dim=vocsize, innerdim=encdim))
        self.dec = SeqDecoder(IdxToOneHot(outvocsize),
                              InConcatCRex(
                                  Attention(attgen, attcon),
                                  GRU(dim=outvocsize+encdim, innerdim=innerdim),
                                  outdim=innerdim))

    def apply(self, inpseq, outseq):    # inpseq: indexes~(batsize, seqlen)
        inp = self.emb(inpseq)  # inp:    floats~(batsize, seqlen, vocsize)
        return self.dec(inp, outseq)


class FwdAttRNNEncDecoder(Block):
    '''
    Take the input index sequence as-is, transform to one-hot, feed to gate AttentionGenerator, encode with weighted SeqEncoder,
    put everything as attention inside SeqDecoder
    '''
    def __init__(self, vocsize=25, outvocsize=20, encdim=200, innerdim=200, attdim=50, **kw):
        super(FwdAttRNNEncDecoder, self).__init__(**kw)
        self.emb = IdxToOneHot(vocsize)
        attgen = LinearGateAttentionGenerator(indim=innerdim+vocsize, innerdim=attdim)
        attcon = SeqEncoder(
            GRU(dim=vocsize, innerdim=encdim))
        self.dec = SeqDecoder(IdxToOneHot(outvocsize),
                              OutConcatCRex(
                                  Attention(attgen, attcon),
                                  GRU(dim=outvocsize, innerdim=innerdim),
                                  outdim=innerdim+encdim))

    def apply(self, inpseq, outseq):    # inpseq: indexes~(batsize, seqlen)
        inp = self.emb(inpseq)  # inp:    floats~(batsize, seqlen, vocsize)
        return self.dec(inp, outseq)


class RewAttSumDecoder(Block):
    def __init__(self, vocsize=25, outvocsize=25, encdim=200, innerdim=200, attdim=50, **kw):
        super(RewAttSumDecoder, self).__init__(**kw)
        self.rnn = ReccableStack(IdxToOneHot(vocsize), GRU(dim=vocsize, innerdim=encdim))
        attgen = LinearGateAttentionGenerator(indim=innerdim+encdim, innerdim=attdim)
        attcon = WeightedSum()
        self.dec = SeqDecoder(IdxToOneHot(outvocsize),
                              InConcatCRex(
                                  Attention(attgen, attcon),
                                  GRU(dim=outvocsize+encdim, innerdim=innerdim),
                                  outdim=innerdim))

    def apply(self, inpseq, outseq):        # inpseq: indexes~(batsize, seqlen)
        rnnout = self.rnn(inpseq)   # (batsize, seqlen, encdim)
        return self.dec(rnnout, outseq)     # (batsize, seqlen, vocsize)


class FwdAttSumDecoder(Block):
    def __init__(self, vocsize=25, outvocsize=25, encdim=300, innerdim=200, attdim=50, **kw):
        super(FwdAttSumDecoder, self).__init__(**kw)
        self.rnn = RecurrentStack(IdxToOneHot(vocsize), GRU(dim=vocsize, innerdim=encdim))
        attgen = LinearGateAttentionGenerator(indim=innerdim+encdim, innerdim=attdim)
        attcon = WeightedSum()
        self.dec = SeqDecoder(IdxToOneHot(outvocsize),
                              OutConcatCRex(
                                  Attention(attgen, attcon),
                                  GRU(dim=outvocsize, innerdim=innerdim),
                                  outdim=innerdim+encdim
                              ))

    def apply(self, inpseq, outseq):
        rnnout = self.rnn(inpseq)
        return self.dec(rnnout, outseq)


class BiFwdAttSumDecoder(Block):
    def __init__(self, vocsize=25, outvocsize=25, encdim=300, innerdim=200, attdim=50, **kw):
        super(BiFwdAttSumDecoder, self).__init__(**kw)
        self.rnn = RecurrentStack(IdxToOneHot(vocsize),
                                  BiRNU.fromrnu(GRU, dim=vocsize, innerdim=encdim))
        attgen = LinearGateAttentionGenerator(indim=innerdim+encdim*2, innerdim=attdim)
        attcon = WeightedSum()
        self.dec = SeqDecoder(IdxToOneHot(outvocsize),
                              OutConcatCRex(
                                  Attention(attgen, attcon),
                                  GRU(dim=outvocsize, innerdim=innerdim),
                                  outdim=innerdim+encdim*2
                              ))

    def apply(self, inpseq, outseq):
        rnnout = self.rnn(inpseq)
        return self.dec(rnnout, outseq)

