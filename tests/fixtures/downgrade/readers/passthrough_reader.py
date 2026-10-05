import sys, json
obj = json.load(sys.stdin)
json.dump(obj, sys.stdout)
