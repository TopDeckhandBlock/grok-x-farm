import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
import builtins
builtins.input = lambda *a: "start"
import grok_register_ttk as g
sys.argv = ["grok_register_ttk.py", "cli"]
g.main()
