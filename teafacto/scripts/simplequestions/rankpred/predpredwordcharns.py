from teafacto.util import argprun, ticktock
from teafacto.procutil import wordmatfromdic
import numpy as np, os, sys, math, pickle
from IPython import embed
from teafacto.core.base import Val, Block, tensorops as T
from teafacto.blocks.basic import VectorEmbed
from teafacto.blocks.seq.enc import SimpleSeq2Vec
from teafacto.blocks.match import CosineDistance, MatchScore
from teafacto.blocks.memory import MemVec

def readdata(p="../../../../data/simplequestions/clean/datamat.word.fb2m.pkl",
             relsperentp="../../../../data/simplequestions/allrelsperent.dmp"):
    tt = ticktock("dataloader")
    tt.tick("loading datamat")
    x = pickle.load(open(p))
    tt.tock("datamat loaded")
    worddic = x["worddic"]
    entdic = x["entdic"]
    numents = x["numents"]
    entmat = x["entmat"]
    traindata, traingold = x["train"]
    validdata, validgold = x["valid"]
    testdata, testgold = x["test"]
    testsubjs = testgold[:, 0]
    testsubjsrels = {k: ([], []) for k in set(list(testsubjs))}

    tt.tick("loading test cans")
    for line in open(relsperentp):
        subj, relsout, relsin = line[:-1].split("\t")
        if subj in entdic and entdic[subj] in testsubjsrels:
            testsubjsrels[entdic[subj]] = (
                [entdic[x] for x in relsout.split(" ")] if relsout != "" else [],
                [entdic[x] for x in relsin.split(" ")] if relsin != "" else []
            )
    tt.tock("test cans loaded")
    # select and offset mats
    traingold = traingold[:, 1] - numents
    validgold = validgold[:, 1] - numents
    testgold = testgold[:, 1] - numents
    entmat = entmat[numents:, :]
    # select and offset entdic
    entdic = {k: v - numents for k, v in entdic.items() if v >= numents}
    # make testrelcans with new idx space
    testrelcans = [([y - numents for y in testsubjsrels[x][0]],
                    [y - numents for y in testsubjsrels[x][1]])
                   for x in testsubjs]


    return (traindata, traingold), (validdata, validgold), (testdata, testgold),\
           worddic, entdic, entmat, testrelcans


class ConcatEmbed(Block):
    def __init__(self, *blocks, **kw):
        self.blocks = blocks
        super(ConcatEmbed, self).__init__(**kw)
        self.outdim = reduce(lambda x, y: x + y, [block.outdim for block in self.blocks], 0)

    def apply(self, idxs):
        axis = idxs.ndim
        blockrets = [block(idxs) for block in self.blocks]
        ret = T.concatenate(blockrets, axis=axis)
        return ret


def run(epochs=50,
        numbats=700,
        lr=1.,
        wreg=0.000001,
        bidir=False,
        layers=1,
        embdim=200,
        encdim=200,
        decdim=200,
        negrate=1,
        margin=1.,
        hingeloss=False,
        debug=False,
        checkdata=False,
        predencode=False,
        wordchar=False,
        ):
    maskid = -1
    tt = ticktock("predpred")
    tt.tick("loading data")
    (traindata, traingold), (validdata, validgold), (testdata, testgold), \
    worddic, entdic, entmat, testsubjsrels = readdata()

    if wordchar:
        wordmat, chardic = wordmatfromdic(worddic)
        numchars = max(chardic.values()) + 1

    tt.tock("data loaded")
    if checkdata:
        rwd = {v: k for k, v in worddic.items()}
        red = {v: k for k, v in entdic.items()}
        def pp(widxs):
            print " ".join([rwd[x] if x in rwd else "" for x in widxs])
        embed()

    numwords = max(worddic.values()) + 1
    numents = max(entdic.values()) + 1

    if bidir:
        encdim = [encdim / 2] * layers
    else:
        encdim = [encdim] * layers

    # word/char model
    wordemb = VectorEmbed(numwords, embdim)
    if wordchar:
        wordenc = MemVec(SimpleSeq2Vec(indim=numchars,
                                inpembdim=50,
                                innerdim=embdim,
                                maskid=-1,
                                bidir=False,
                                layers=1)
                         )
        wordenc.load(wordmat)
        wordemb = ConcatEmbed(wordemb, wordenc)

    # question-side model
    question_enc = SimpleSeq2Vec(inpemb=wordemb,
                                 inpembdim=wordemb.outdim,
                                 innerdim=encdim,
                                 maskid=maskid,
                                 bidir=bidir,
                                 layers=layers)

    # predicate-side model
    if predencode:
        predemb = MemVec(SimpleSeq2Vec(inpemb=wordemb,
                                inpembdim=wordemb.outdim,
                                innerdim=decdim,
                                maskid=maskid,
                                bidir=bidir,
                                layers=layers)
                         )
        predemb.load(entmat)
    else:
        predemb = VectorEmbed(numents, decdim)

    # scoring
    scorer = MatchScore(question_enc, predemb, scorer=CosineDistance())

    class NegIdxGen(object):
        def __init__(self, rng):
            self.min = 0
            self.max = rng

        def __call__(self, datas, gold):
            predrand = np.random.randint(self.min, self.max, (gold.shape[0],))
            return datas, predrand.astype("int32")

    if hingeloss:
        obj = lambda p, n: (n - p + margin).clip(0, np.infty)
    else:
        obj = lambda p, n: n - p

    tt.tick("training")
    nscorer = scorer.nstrain([traindata, traingold]) \
                .negsamplegen(NegIdxGen(numents)) \
                .negrate(negrate) \
                .objective(obj) \
                .adagrad(lr=lr).l2(wreg).grad_total_norm(1.0)\
                .validate_on([validdata, validgold])\
        .train(numbats=numbats, epochs=epochs)
    tt.tock("trained")

    # evaluation
    tt.tick("evaluating")
    qenc_pred = question_enc.predict(testdata)
    scores = []
    for i in range(qenc_pred.shape[0]):
        cans = testsubjsrels[i][0] #+ testsubjsrels[i][1]
        if len(cans) == 0:
            scores.append([(-1, -np.infty)])
            continue
        canembs = predemb.predict(cans)
        scoresi = scorer.s.predict(np.repeat(qenc_pred[np.newaxis, i],
                                             canembs.shape[0], axis=0),
                                   canembs)
        scores.append(zip(cans, scoresi))
        tt.progress(i, qenc_pred.shape[0], live=True)
    sortedbest = [sorted(cansi, key=lambda (x, y): y, reverse=True) for cansi in scores]
    best = [sortedbesti[0][0] for sortedbesti in sortedbest]
    # Accuracy
    accuracy = np.sum(best == testgold) * 1. / testgold.shape[0]


    print("Accuracy: {}%".format(accuracy * 100))


if __name__ == "__main__":
    argprun(run)