import sys, json
obj = json.load(sys.stdin)
for item in obj.get('items', []):
    item.pop('_canonical_v3', None)
json.dump(obj, sys.stdout)
