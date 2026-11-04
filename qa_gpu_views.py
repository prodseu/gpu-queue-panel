"""Compatibility entry for isolated multiuser browser checks."""
import json
from qa_multiuser import check
if __name__=='__main__':print(json.dumps(check(),ensure_ascii=False,indent=2))
