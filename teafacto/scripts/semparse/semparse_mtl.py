from teafacto.scripts.semparse.semparse import loadgeo, preprocess, split_train_test
from teafacto.util import argprun, isstring, issequence, ticktock
from teafacto.procutil import wordids2string
import numpy as np, re, random
from IPython import embed

from teafacto.blocks.cnn import CNNSeqEncoder
from teafacto.blocks.seq.encdec import EncDec
from teafacto.blocks.word import WordEmb
from teafacto.blocks.seq.attention import Attention
from teafacto.blocks.basic import SMO
from teafacto.blocks.activations import ReLU, Tanh

from teafacto.core.base import asblock

def run(numbats=50,
        epochs=10,
        lr=0.5,
        embdim=50,
        encdim=200,
        dropout=0.3,
        inconcat=True,
        outconcat=True,
        concatdecinp=False,
        forwardattention=False,
        splitatt=False,
        preproc=True,
        posembdim=50,
        userelu=False,
        numdeclayers=1,
        inspectdata=False,
        ):
    tt = ticktock("script")

    tt.tick("loading data")
    maskid = 0
    qmat, amat, qdic, adic, qwc, awc = loadgeo(reverse=False)
    tt.tock("data loaded")

    def pp(i):
        print wordids2string(qmat[i], {v: k for k, v in qdic.items()}, 0)
        print wordids2string(amat[i], {v: k for k, v in adic.items()}, 0)

    if preproc:
        tt.tick("preprocessing")
        qmat, amat, qdic, adic, qwc, awc = preprocess(qmat, amat, qdic, adic, qwc, awc, maskid, qreversed=False,
                                                  dorare=True)
        tt.tock("preprocessed")

    qmat_train, qmat_test = split_train_test(qmat)
    amat_train, amat_test = split_train_test(amat)

    if inspectdata:
        embed()

    inpemb = WordEmb(worddic=qdic, maskid=maskid, dim=embdim)
    outemb = WordEmb(worddic=adic, maskid=maskid, dim=embdim)

    encoder = CNNSeqEncoder(inpemb=inpemb,
                            numpos=qmat.shape[1],
                            posembdim=posembdim,
                            innerdim=[encdim] * 4 if not splitatt else [encdim*2] * 4,
                            window=[3, 3, 5, 5],
                            activation=ReLU if userelu else Tanh,
                            dropout=dropout).all_outputs()

    smodim = encdim+encdim if not concatdecinp else encdim+encdim+embdim
    ctxdim = encdim
    critdim = encdim if not concatdecinp else encdim + embdim
    splitters = (asblock(lambda x: x[:, :, :encdim]), asblock(lambda x: x[:, :, encdim:encdim*2]))
    attention = Attention(splitters=splitters) if splitatt else Attention()
    attention.forward_gen(critdim, ctxdim, encdim) if forwardattention else attention.dot_gen()

    decoder = EncDec(encoder=encoder,
                     attention=attention,
                     inpemb=outemb,
                     indim=embdim+encdim,
                     inconcat=inconcat, outconcat=outconcat, concatdecinp=concatdecinp,
                     innerdim=[encdim]*numdeclayers,
                     dropout_h=dropout,
                     dropout_in=dropout,
                     smo=SMO(smodim, max(adic.values()) + 1))

    tt.tick("training")

    decoder.train([amat_train[:, :-1], qmat_train], amat_train[:, 1:]) \
        .cross_entropy().adadelta(lr=lr).grad_total_norm(5.) \
        .validate_on([amat_test[:, :-1], qmat_test], amat_test[:, 1:]) \
        .cross_entropy().seq_accuracy() \
        .train(numbats, epochs)

    tt.tock("trained")
    embed()

if __name__ == "__main__":
    argprun(run)