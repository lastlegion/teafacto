from teafacto.util import argprun, isstring, issequence
from teafacto.procutil import wordids2string, wordmat2charmat
import numpy as np, re
from IPython import embed

from teafacto.core.base import Val, Block, tensorops as T
from teafacto.blocks.seq.rnu import GRU
from teafacto.blocks.seq.encdec import SimpleSeqEncDecAtt
from teafacto.blocks.basic import VectorEmbed, Linear, MatDot
from teafacto.blocks.activations import Softmax, Tanh
from teafacto.blocks.lang.wordvec import WordEmb, Glove


def loadgeopl(p="../../../data/semparse/geoquery.txt", customemb=False, reverse=True):
    qss, ass = [], []
    maxqlen, maxalen = 0, 0
    qwords, awords = {}, {}

    if isstring(p):
        p = open(p)

    for line in p:
        splitre = "[\s-]" if customemb else "\s"
        q, a = [re.split(splitre, x) for x in line[:-1].split("\t")]
        q = ["<s>"] + q + ["</s>"]
        a = ["<s>"] + a + ["</s>"]
        qss.append(q)
        ass.append(a)
        maxqlen = max(len(q), maxqlen)
        maxalen = max(len(a), maxalen)
        for qw in q:
            if qw not in qwords:
                qwords[qw] = 0
            qwords[qw] += 1
        for aw in a:
            if aw not in awords:
                awords[aw] = 0
            awords[aw] += 1
    qmat = np.zeros((len(qss), maxqlen), dtype="int32")
    amat = np.zeros((len(ass), maxalen), dtype="int32")
    qdic = dict(zip([x for x, y in sorted(qwords.items(), reverse=True, key=lambda (x, y): y)],
                    range(1, len(qwords) + 1)))
    adic = dict(zip([x for x, y in sorted(awords.items(), reverse=True, key=lambda (x, y): y)],
                    range(1, len(awords) + 1)))
    for i in range(len(qss)):
        q = qss[i]
        a = ass[i]
        qx = [qdic[x] for x in q]
        if reverse:
            qx.reverse()
        qmat[i, :len(q)] = qx
        amat[i, :len(a)] = [adic[x] for x in a]
    return qmat, amat, qdic, adic, qwords, awords


def loadgeo(trainp="../../../data/semparse/geoquery.lbd.dev",
            testp="../../../data/semparse/geoquery.lbd.test",
            customemb=False,
            reverse=True):

    d = []

    def fixbrackets(m):
        ret = ""
        if len(m.group(1)) > 0:
            ret += m.group(1)
            ret += " "
        ret += m.group(2)
        if len(m.group(3)) > 0:
            ret += " "
            ret += m.group(3)
        return ret

    def addlines(p, d):
        curline = ""
        for line in open(p):
            if len(curline) == 0:
                curline = line
            else:
                if line == "\n":
                    d.append(""+curline)
                    curline = ""
                elif line[:2] == "//":
                    pass
                else:
                    oldline = line
                    line = line[:-1]
                    while oldline != line:
                        oldline = line
                        line = re.sub("([^\s]?)([()])([^\s]?)",
                                         fixbrackets,
                                         line)
                    curline = "{}\t{}".format(curline, line)

    addlines(trainp, d)
    addlines(testp, d)

    return loadgeopl(p=d, customemb=customemb, reverse=reverse)


class VectorPosEmb(Block):
    def __init__(self, baseemb, numpos, posembdim, **kw):
        super(VectorPosEmb, self).__init__(**kw)
        self.baseemb = baseemb
        self.pemb = VectorEmbed(indim=numpos, dim=posembdim)
        self.outdim = self.baseemb.outdim + self.pemb.outdim
        self.indim = self.baseemb.indim

    def apply(self, x):     # (batsize, seqlen, 2)
        wembeddings = self.baseemb(x[:, :, 0])
        pembeddings = self.pemb(x[:, :, 1])
        ret = T.concatenate([wembeddings, pembeddings], axis=2)     # (batsize, seqlen, wembdim+pembdim)
        ret.mask = wembeddings.mask
        return ret


class SoftMaxOut(Block):
    def __init__(self, indim=None, innerdim=None, outvocsize=None, dropout=None, **kw):
        super(SoftMaxOut, self).__init__(**kw)
        self.indim, self.innerdim, self.outvocsize = indim, innerdim, outvocsize
        self.lin1 = Linear(indim=indim, dim=innerdim, dropout=dropout)
        self.lin2 = MatDot(indim=innerdim, dim=outvocsize)

    def apply(self, x):
        a = self.lin1(x)
        b = Tanh()(a)
        c = self.lin2(b)
        d = Softmax()(c)
        return d

    def setlin2(self, v):
        self.lin2 = MatDot(indim=self.indim, dim=self.innerdim, value=v)


def preprocess(qmat, amat, qdic, adic, qwc, awc, maskid, qreversed=False):
    amat[amat == adic["capital:c"]] = adic["capital:t"]
    replaceina = set()
    for k in adic:
        if (k[-2:] in ":c :s :r :m :n".split() or
            k[-3:] in ":lo :co".split()) and not k == "capital:c":
            replaceina.add(k)
    for r in replaceina:
        splits = r.split(":")
        rt = splits[1]+"-type"
        if not rt in adic:
            adic[rt] = max(adic.values()) + 1
        if not rt in qdic:
            qdic[rt] = max(qdic.values()) + 1
    radic = {v: k for k, v in adic.items()}
    rqdic = {v: k for k, v in qdic.items()}
    for i in range(qmat.shape[0]):
        for j in range(amat.shape[1]):
            if amat[i, j] in {adic[x] for x in replaceina}:
                sf = radic[amat[i, j]].split(":")[0].split("_")
                #if sf[-1] == "river" or len(sfs[0][-1]) == 2:
                #    sf = sf[:-1]
                sft = radic[amat[i, j]].split(":")[1]
                amat[i, j] = adic[sft+"-type"]
                sfs = [sf]
                qmati = qmat[i]
                if qreversed:
                    qmatio = maskid * np.ones_like(qmati)
                    m = qmati.shape[0] - 1
                    n = 0
                    while m >= 0:
                        if qmati[m] == maskid:
                            pass
                        else:
                            qmatio[n] = qmati[m]
                            n += 1
                        m -= 1
                    qmati = qmatio
                if sf == ["usa"]:
                    sfs.append("united states".split())
                    sfs.append("the country".split())
                    sfs.append("the states".split())
                    sfs.append(["us"])
                for sf in sfs:
                    k = 0
                    while k < qmat.shape[1]:
                        if qmati[k] != maskid and \
                                        rqdic[qmati[k]] == sf[0]:
                            l = 0
                            while l < len(sf) and l + k < qmat.shape[1]:
                                if rqdic[qmati[k + l]] == sf[l]:
                                    l += 1
                                else:
                                    break
                            if l >= len(sf) - 1:
                                qmati[k] = qdic[sft+"-type"]
                                qmati[k+1:qmat.shape[1]-l+1] = qmati[k+l:]
                                qmati[qmat.shape[1]-l+1:] = maskid
                        k += 1
                if qreversed:
                    qmatio = maskid * np.ones_like(qmati)
                    m = qmati.shape[0] - 1
                    n = 0
                    while m >= 0:
                        if qmati[m] == maskid:
                            pass
                        else:
                            qmatio[n] = qmati[m]
                            n += 1
                        m -= 1
                    qmati = qmatio
                qmat[i] = qmati
    def pp(i):
        print wordids2string(qmat[i], {v: k for k, v in qdic.items()})
        print wordids2string(amat[i], {v: k for k, v in adic.items()})
    # test
    wop = []
    for i in range(qmat.shape[0]):
        if "-type" in wordids2string(amat[i], {v: k for k, v in adic.items()}) and \
            "-type" not in wordids2string(qmat[i], {v: k for k, v in qdic.items()}):
            wop.append(i)
    print "{}/{}".format(len(wop), qmat.shape[0])
    # rare words
    #print qwc
    #embed()

    return qmat, amat, qdic, adic, qwc, awc



def run(
        numbats=50,
        epochs=10,
        lr=1.,
        embdim=50,
        encdim=400,
        dropout=0.2,
        layers=2,
        inconcat=True,
        outconcat=True,
        posemb=False,
        customemb=False,
        charlevel=False,
        preproc=True,
        corruptnoise=0.0):

    #TODO: multilayer gets shape mismatch error ERROR!!!
    #TODO: Dong's preprocessing
    #       - rare words index = maskid in word-level preprocessing
    #TODO: bi-encoder and other beasts
    # loaddata
    qmat, amat, qdic, adic, qwc, awc = loadgeo(customemb=customemb, reverse=not charlevel)
    maskid = 0
    if preproc:
        qmat, amat, qdic, adic, qwc, awc = preprocess(qmat, amat, qdic, adic, qwc, awc, maskid, qreversed=not charlevel)

    if charlevel:
        qmat = wordmat2charmat(qmat, qdic, maxlen=1000, maskid=maskid)
        amat = wordmat2charmat(amat, adic, maxlen=1000, maskid=maskid)
        qmat[qmat > 0] += 2
        amat[amat > 0] += 2
        qdic = dict([(chr(x), x + 2) for x in range(np.max(qmat))])
        adic = dict([(chr(x), x + 2) for x in range(np.max(amat))])
        qdic.update({"<RARE>": 1})
        adic.update({"<RARE>": 1})
        print wordids2string(qmat[0], {v: k for k, v in qdic.items()})
        print wordids2string(amat[0], {v: k for k, v in adic.items()})

    np.random.seed(12345)

    #embed()

    encdimi = [encdim] * layers
    decdimi = [encdim] * layers


    inpemb = WordEmb(worddic=qdic, maskid=maskid, dim=embdim)
    outemb = WordEmb(worddic=adic, maskid=maskid, dim=embdim)

    if customemb:
        thresh = 10
        sawc = sorted(awc.items(), key=lambda (k, v): v, reverse=True)
        rarewords = {k for (k, v) in sawc if v < thresh}
        g = Glove(embdim)
        inpemb = inpemb.override(g)
        outemb = outemb.override(g, which=rarewords)

    if posemb:      # custom emb layers, with positional embeddings
        posembdim = 50
        inpemb = VectorPosEmb(inpemb, qmat.shape[1], posembdim)
        outemb = VectorPosEmb(outemb, amat.shape[1], posembdim)

    smodim = embdim
    smo = SoftMaxOut(indim=encdim+encdim, innerdim=smodim,
                     outvocsize=len(adic)+1, dropout=dropout)

    if customemb:
        smo.setlin2(outemb.baseemb.W.T)

    # make seq/dec+att
    encdec = SimpleSeqEncDecAtt(inpvocsize=max(qdic.values()) + 1,
                                inpembdim=embdim,
                                inpemb=inpemb,
                                outvocsize=max(adic.values()) + 1,
                                outembdim=embdim,
                                outemb=outemb,
                                encdim=encdimi,
                                decdim=decdimi,
                                maskid=maskid,
                                statetrans=True,
                                dropout=dropout,
                                inconcat=inconcat,
                                outconcat=outconcat,
                                rnu=GRU,
                                vecout=smo,
                                )

    amati = amat

    class RandomCorrupt(object):
        def __init__(self, p = 0.1, corruptdecoder=None, corruptencoder=None, maskid=0):
            self.corruptdecoder = corruptdecoder    # decoder corrupt range
            self.corruptencoder = corruptencoder    # encoder corrupt range
            self.p = p
            self.maskid = maskid

        def __call__(self, encinp, decinp, gold, phase=None):
            if phase == "TRAIN":
                if self.corruptencoder is not None:
                    encinp = self._corruptseq(encinp, self.corruptencoder)
                if self.corruptdecoder is not None:
                    decinp = self._corruptseq(decinp, self.corruptdecoder)
            return encinp, decinp, gold


        def _corruptseq(self, seq, range):
            if self.p > 0:
                corrupt = np.random.randint(range[0], range[1], seq.shape, dtype="int32")
                mask = np.random.random(seq.shape) < self.p
                seqmask = seq != self.maskid
                mask = np.logical_and(mask, seqmask)
                outp = ((1 - mask) * seq + mask * corrupt).astype("int32")
                #embed()
            else:
                outp = seq
            return outp




    if posemb:
        qposmat = np.arange(0, qmat.shape[1])[None, :]
        qposmat = np.repeat(qposmat, qmat.shape[0], axis=0)
        qmat = np.concatenate([qmat[:, :, None], qposmat[:, :, None]], axis=2)
        aposmat = np.arange(0, amat.shape[1])[None, :]
        aposmat = np.repeat(aposmat, amat.shape[0], axis=0)
        amati = np.concatenate([amat[:, :, None], aposmat[:, :, None]], axis=2)

    tqmat = qmat[:600]
    tamat = amat[:600]
    tamati = amati[:600]
    xqmat = qmat[600:]
    xamat = amat[600:]
    xamati = amati[600:]

    #embed()

    encdec.train([tqmat, tamati[:, :-1]], tamat[:, 1:])\
        .sampletransform(RandomCorrupt(corruptdecoder=(2, max(adic.values()) + 1),
                                       corruptencoder=(2, max(qdic.values()) + 1),
                                       maskid=maskid, p=corruptnoise))\
        .cross_entropy().rmsprop(lr=lr/numbats).grad_total_norm(1.)\
        .validate_on([xqmat, xamati[:, :-1]], xamat[:, 1:]).cross_entropy().seq_accuracy()\
        .train(numbats, epochs)

    qrwd = {v: k for k, v in qdic.items()}
    arwd = {v: k for k, v in adic.items()}

    def play(*x, **kw):
        hidecorrect = False
        if "hidecorrect" in kw:
            hidecorrect = kw["hidecorrect"]
        if len(x) == 1:
            x = x[0]
            q = wordids2string(xqmat[x], rwd=qrwd, maskid=maskid, reverse=True)
            ga = wordids2string(xamat[x, 1:], rwd=arwd, maskid=maskid)
            pred = encdec.predict(xqmat[x:x+1], xamati[x:x+1, :-1])
            pa = wordids2string(np.argmax(pred[0], axis=1), rwd=arwd, maskid=maskid)
            if hidecorrect and ga == pa[:len(ga)]:  # correct
                return False
            else:
                print "{}: {}".format(x, q)
                print ga
                print pa
                return True
        elif len(x) == 0:
            for i in range(0, xqmat.shape[0]):
                r = play(i)
                if r:
                    raw_input()
        else:
            raise Exception("invalid argument to play")

    embed()

if __name__ == "__main__":
    argprun(run)