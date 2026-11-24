'''Trusted installation entry point; invoke by absolute path with python -I -B.'''

from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    if not sys.flags.isolated:
        sys.exit('claim launcher requires python -I')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    runpy.run_module('tools.claim_issue', run_name='__main__')
