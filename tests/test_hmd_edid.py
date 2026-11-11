# SPDX-License-Identifier: MIT
import importlib.util
from pathlib import Path
import unittest
import uuid

spec=importlib.util.spec_from_file_location('hmd_edid',Path(__file__).resolve().parents[1]/'tools/psvr_native_analysis/hmd_edid.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def checksum(block):
    block[127]=(-sum(block[:127]))&255
    return block

def fixture():
    base=bytearray(128)
    base[:8]=bytes.fromhex('00ffffffffffff00')
    base[8:12]=bytes.fromhex('4dd9046a')
    base[54:72]=bytes.fromhex('023a801871382d40582c4500a05a0000001e')
    base[126]=1
    cta=bytearray(128)
    cta[:4]=bytes([2,3,12,0xc0])
    cta[4:12]=bytes([0x43,16,31,63,0x23,9,7,7])
    cta[12:30]=base[54:72]
    return bytes(checksum(base)+checksum(cta))

class HmdEdidTests(unittest.TestCase):
    def test_identity_audio_video_timings_and_checksums(self):
        source=fixture()
        guid=uuid.UUID('12345678-1234-5678-9abc-123456789abc')
        result=module.add_hmd_block(source,guid)
        self.assertEqual(result[:128],source[:128])
        self.assertEqual(result[132:140],source[132:140])
        self.assertEqual(result[162:180],source[140:158])
        self.assertEqual(result[140:162],bytes([0x75,0x5c,0x12,0xca,2,7])+guid.bytes_le)
        self.assertEqual(result[130],34)
        self.assertEqual(sum(result[128:256])%256,0)
    def test_duplicate_block_refused(self):
        result=module.add_hmd_block(fixture(),uuid.uuid4())
        with self.assertRaisesRegex(ValueError,'already present'):
            module.add_hmd_block(result,uuid.uuid4())
    def test_compatibility_variant_only_changes_usage_and_checksum(self):
        guid=uuid.uuid4()
        normal=module.add_hmd_block(fixture(),guid)
        compat=module.add_hmd_block(fixture(),guid,True)
        changes=[i for i,(a,b) in enumerate(zip(normal,compat)) if a!=b]
        self.assertEqual(changes,[145,255])
        self.assertEqual(compat[145],0x27)
        self.assertEqual(sum(compat[128:256])%256,0)
    def test_corrupt_wrong_device_incomplete_and_full_payload_refused(self):
        for case in ('checksum','identity','incomplete','full'):
            data=bytearray(fixture())
            if case=='checksum': data[20]^=1
            if case=='identity': data[10]^=1; data[:128]=checksum(data[:128])
            if case=='incomplete': data=data[:128]
            if case=='full': data[233]=1; data[128:]=checksum(data[128:])
            with self.subTest(case=case),self.assertRaises(ValueError):
                module.add_hmd_block(bytes(data),uuid.uuid4())

if __name__=='__main__': unittest.main()
