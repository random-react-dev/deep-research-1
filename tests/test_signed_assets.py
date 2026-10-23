import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('sign_setup',ROOT/'tools/sign_setup.py')
signer=importlib.util.module_from_spec(spec);spec.loader.exec_module(signer)
code=(ROOT/'server/setup-install.md').read_text().split('```python\n')[1].split('```')[0]
namespace={'__name__':'test_fixture'}
exec(compile(code,'signed-template','exec'),namespace)

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/'install'
    def package(self,name='bin/helper',mode=stat.S_IFREG|0o755,data=b'fixture',duplicate=False):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            item=zipfile.ZipInfo(name);item.create_system=3;item.external_attr=mode<<16
            z.writestr(item,data)
            if duplicate:z.writestr(item,data)
        manifest={'files':[{'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'mode':'0755'}]}
        return output.getvalue(),manifest
    def test_valid_and_idempotent(self):
        data,manifest=self.package()
        namespace['safe_extract'](data,manifest,self.root)
        namespace['safe_extract'](data,manifest,self.root)
        self.assertEqual((self.root/'bin/helper').read_bytes(),b'fixture')
    def test_hostile_paths_and_types(self):
        for name in ('/outside','../outside','bin/../outside','bin\\outside','bin//outside'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):namespace['safe_extract'](*self.package(name),self.root)
        for mode in (stat.S_IFLNK|0o777,stat.S_IFIFO|0o600,stat.S_IFCHR|0o600):
            with self.assertRaises(ValueError):namespace['safe_extract'](*self.package(mode=mode),self.root)
    def test_duplicate_unlisted_digest_size_and_expansion(self):
        with self.assertRaises(ValueError):namespace['safe_extract'](*self.package(duplicate=True),self.root)
        for field,value in (('path','different'),('size',99),('sha256','0'*64),('size',129*1024*1024)):
            data,manifest=self.package();manifest['files'][0][field]=value
            with self.assertRaises(ValueError):namespace['safe_extract'](data,manifest,self.root)
    def test_linked_destinations_rejected(self):
        self.root.mkdir();outside=self.root.parent/'outside';outside.mkdir()
        (self.root/'bin').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):namespace['safe_extract'](*self.package(),self.root)
        (self.root/'bin').unlink();(self.root/'bin').mkdir()
        original=outside/'original';original.write_bytes(b'fixture');os.link(original,self.root/'bin/helper')
        with self.assertRaises(ValueError):namespace['safe_extract'](*self.package(),self.root)

    def test_optional_actual_release(self):
        path=os.environ.get('HELPER_RUNNER_TEST_RELEASE')
        if not path:self.skipTest('Set HELPER_RUNNER_TEST_RELEASE for real release extraction')
        path=Path(path);manifest=json.loads(path.with_suffix('.json').read_text())
        namespace['safe_extract'](path.read_bytes(),manifest,self.root)
        self.assertEqual(len([p for p in self.root.rglob('*') if p.is_file()]),6)

    def test_download_retries_partial_and_rejects_oversize(self):
        import http.client
        data=b'correct archive';digest=hashlib.sha256(data).hexdigest()
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,limit):return data
        class Opener:
            calls=0
            def open(self,*_,**__):
                self.calls+=1
                if self.calls==1:raise http.client.IncompleteRead(b'part',99)
                return Response()
        opener=Opener()
        with patch.object(namespace['urllib'].request,'build_opener',return_value=opener),patch.object(namespace['time'],'sleep'):
            self.assertEqual(namespace['download_archive']('https://example.test/x',len(data),digest),data)
            self.assertEqual(opener.calls,2)
            with self.assertRaises(ValueError):namespace['download_archive']('https://example.test/x',1,digest)

class SigningTests(unittest.TestCase):
    def test_signature_and_immutable_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);key=root/'key.pem';public=root/'pub.pem'
            subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(key)],check=True,capture_output=True)
            subprocess.run(['openssl','pkey','-in',str(key),'-pubout','-out',str(public)],check=True,capture_output=True)
            release=root/'release.json';release.write_text('{"model":"abliterated-model-large-flash"}')
            models=root/'models.json';models.write_text('{"models":[{"slug":"abliterated-model-large-flash"}]}')
            output=root/'signed.json'
            args=(key,release,models,ROOT/'server/setup-install.md',ROOT/'server/setup-reuse.md','test-1',output)
            payload=signer.sign_setup(*args,now=1000)
            signer.sign_setup(*args,now=1000)
            envelope=json.loads(output.read_text());raw=base64.b64decode(envelope['payload'])
            signature=root/'sig';signature.write_bytes(base64.b64decode(envelope['signature']))
            result=subprocess.run(['openssl','dgst','-sha256','-verify',str(public),'-signature',str(signature)],input=raw,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(payload['expires_at'],1000+90*86400)
            self.assertEqual(payload['model_catalog_sha256'],hashlib.sha256(signer.canonical(json.loads(models.read_text()))).hexdigest())
            with self.assertRaises(ValueError):signer.sign_setup(*args,now=1001)

if __name__=='__main__':unittest.main()
