# SPDX-License-Identifier: MIT
"""Read-only EDID comparison; reports identity, checksums and changed offsets."""
import argparse
import hashlib
import json
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('original',type=Path)
parser.add_argument('candidate',type=Path)
args=parser.parse_args()
a=args.original.read_bytes()
b=args.candidate.read_bytes()
def describe(data):
    vendor=int.from_bytes(data[8:10],'big')
    manufacturer=''.join(chr(((vendor>>shift)&31)+64) for shift in (10,5,0))
    return dict(length=len(data),sha256=hashlib.sha256(data).hexdigest(),manufacturer=manufacturer,
                api_vendor=int.from_bytes(data[8:10],'little'),product=int.from_bytes(data[10:12],'little'),
                extension_count=data[126],checksums=[sum(data[i:i+128])%256 for i in range(0,len(data),128)],
                extension_hex=[data[i:i+128].hex() for i in range(128,len(data),128)],
                descriptor_hex=[data[i:i+18].hex() for i in (54,72,90,108)])
print(json.dumps(dict(original=describe(a),candidate=describe(b),
                     changes=[dict(offset=i,old=x,new=y) for i,(x,y) in enumerate(zip(a,b)) if x!=y]),indent=2))
