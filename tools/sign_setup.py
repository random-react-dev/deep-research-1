#!/usr/bin/env python3
"""Offline developer signing; private keys never enter the skill or server package."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')


def sign_setup(key, release, models, install, reuse, revision, output, now=None, expiry_days=90):
    now = int(time.time()) if now is None else int(now)
    if not 1 <= expiry_days <= 365: raise ValueError('Expiry must be 1 through 365 days')
    catalog = json.loads(Path(models).read_text())
    manifest = json.loads(Path(release).read_text())
    model = 'abliterated-model-large-flash'
    if manifest.get('model') != model or catalog['models'][0]['slug'] != model: raise ValueError('Model mismatch')
    payload = {'schema':1, 'revision':revision, 'issued_at':now, 'expires_at':now+expiry_days*86400,
               'model':model,'release':manifest,'model_catalog_sha256':hashlib.sha256(canonical(catalog)).hexdigest(),
               'install_instructions':Path(install).read_text(), 'reuse_instructions':Path(reuse).read_text()}
    if not revision or not payload['install_instructions'].strip() or not payload['reuse_instructions'].strip(): raise ValueError('Empty signed instruction or revision')
    raw = canonical(payload)
    signature = subprocess.run(['openssl','dgst','-sha256','-sign',str(key)], input=raw, capture_output=True, check=True).stdout
    envelope = canonical({'payload':base64.b64encode(raw).decode(),'signature':base64.b64encode(signature).decode()}) + b'\n'
    output=Path(output)
    if output.exists():
        if output.is_symlink() or output.read_bytes()!=envelope: raise ValueError('Refusing to overwrite a different signed envelope')
    else:
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(envelope);stream.flush();os.fsync(stream.fileno())
    return payload


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('key','release','models','install','reuse','output'): p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--revision',required=True);p.add_argument('--expiry-days',type=int,default=90)
    a=p.parse_args()
    payload=sign_setup(a.key,a.release,a.models,a.install,a.reuse,a.revision,a.output,expiry_days=a.expiry_days)
    print(json.dumps({'revision':payload['revision'],'expires_at':payload['expires_at'],'output':str(a.output)}))

if __name__=='__main__':main()
