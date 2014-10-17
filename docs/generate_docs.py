#!/usr/bin/python3
# encoding: utf-8
"""
This scripts generates Veles project documentation
To generate the docs, you have to install sphinx3-doc and the extensions:
    sphinxcontrib-napoleon, sphinx.ext.mathjax and sphinx.ext.argparse
"""

import os
import subprocess
import sys


if __name__ == "__main__":
    docs_path = os.path.dirname(os.path.realpath(__file__))
    docs_source_path = os.path.join(docs_path, "source")
    project_path = os.path.realpath(os.path.join(docs_path, ".."))

    sys.path.append(docs_path)
    os.chdir(docs_path)

    subprocess.call(["./deps.sh"])
    subprocess.call(["sphinx-apidoc", "-e", "-f", "-H",
                    "Source Code", "-o", docs_source_path,
                    project_path, os.path.join(project_path, "veles/external"),
                    os.path.join(project_path, "veles/znicz/external"),
                    os.path.join(project_path, "veles/tests"),
                    os.path.join(project_path, "veles/znicz/tests")])
    subprocess.call(["sphinx-build", "-b", "html", "-d",
                     os.path.join(docs_path, "build/doctrees"),
                     docs_source_path, os.path.join(docs_path, "build/html")])
