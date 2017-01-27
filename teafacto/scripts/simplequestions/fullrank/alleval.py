import sys, os, re
from textwrap import dedent
from teafacto.util import argprun
from collections import OrderedDict

def main(scriptname="testrunscript.py",
         modelfilepattern="testmodelfile{}.txt",
         numtestcans="5,10,400",
         multiprune="0,1",
         mode="concat,seq,multi,multic"):
    if not os.path.exists("alleval"):
        os.makedirs("alleval")
    loc = locals()
    griddict = OrderedDict({x: loc[x].split(",") for x in "numtestcans multiprune mode".split()})
    #print griddict
    for filename in os.listdir("."):
        m = re.match("^{}$".format(modelfilepattern.format("(\d{0,4})\.?(\d{0,3}ep)?")), filename)
        if m:
            #print m.groups()
            modelname = m.group(1)
            if m.group(2) is not None:
                modelname += ",{}".format(m.group(2))
            for i in range(reduce(lambda x, y: x * y, map(len, griddict.values()))):
                indexes = OrderedDict()
                for k, v in griddict.items():
                    indexes[k] = i % len(v)
                    i //= len(v)
                #print indexes
                options = "".join(["-{} {} ".format(x, griddict[x][indexes[x]]) for x in griddict.keys()])
                cmd = """python {}
                            -loadmodel {}
                            {}"""\
                    .format(scriptname,
                            modelname,
                            options
                            )
                cmd = re.sub("\n", "", cmd)
                cmd = re.sub("\s{2,}", " ", cmd)
                print cmd
                targetname = "alleval/{}.out".format(re.sub("\s", "_", cmd))
                os.system("echo {} > {}".format(cmd, targetname))
                os.system("{} >> {} 2>&1".format(cmd, targetname))


if __name__ == "__main__":
    argprun(main)

