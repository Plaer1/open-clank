"""Private fixed child launcher; same process becomes the retained pdflatex."""
import os
import resource
import sys

MAX_OUTPUT_FILE = 32 * 1024 * 1024

def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit(): raise SystemExit(126)
    fd = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_OUTPUT_FILE,MAX_OUTPUT_FILE))
    os.execve(f'/proc/self/fd/{fd}', ['pdflatex','-progname=pdflatex','-fmt=pdflatex','-no-shell-escape',
        '-interaction=nonstopmode','-halt-on-error','-file-line-error','-no-parse-first-line','-no-mktex=tex',
        '-no-mktex=tfm','-no-mktex=pk','-jobname=output','-output-directory=.', 'main.tex'], {'PATH':os.defpath})

if __name__ == '__main__': main()
