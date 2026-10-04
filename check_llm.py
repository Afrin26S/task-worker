from run import load_env
load_env()
from taskworker.llm import make_llm

llm = make_llm()
print("Using model:", getattr(llm, "model", "?"))
print(llm.complete("Reply with a JSON object only.", 'Return {"ok": true}'))