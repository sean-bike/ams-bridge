"""PSF/VCD source semantics, windows and bounded PSFXL batch transfer."""
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from ams_bridge import BridgeError
from ams_bridge.records import metadata
from ams_bridge.wave_data import decode, detect
from ams_bridge.wave_query import selected_points
from ams_bridge.waveforms import parse_local, query_spec
import io
import tarfile
from contextlib import nullcontext


LIMITS = {"points": 1000, "bytes": 1000000, "timeout_s": 10}

VCD = '''$timescale 1 ps $end
$scope module tb $end
$var wire 4 ! bus [3:0] $end
$var wire 4 ! alias [0:3] $end
$var reg 1 # d $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
b0 !
0#
$end
#5
b10xz !
1#
0#
#6
bz !
#10
b1111 !
#20
'''

PSF = '''HEADER
"PSFversion" "1.00"
"analysis type" "tran"
"analysis name" "tran"
TYPE
"sweep" FLOAT DOUBLE
"voltage" FLOAT DOUBLE PROP("units" "V")
SWEEP
"time" "sweep" PROP("units" "s")
TRACE
"out" "voltage"
VALUE
"time" 0
"out" 0
"time" 1
"out" 2
"time" 3
"out" 4
END
'''

class Waveforms(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def data(self, text=VCD, fmt="vcd"):
        path = self.root / "wrong.extension"
        path.write_text(text)
        return decode(path, fmt, LIMITS)

    def error(self, code, func, *args, **kwargs):
        with self.assertRaises(BridgeError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_content_detection_ignores_extension_and_refuses_conflicts(self):
        self.data()
        path = self.root / "wrong.extension"
        self.assertEqual(detect(path), "vcd")
        self.error("format_mismatch", decode, path, "psf_ascii", LIMITS)
        path.write_bytes(b"unknown")
        self.error("unknown_format", detect, path)
        path.write_text('HEADER\n"PSFversion" "1.00"\n"analysisInst" STRUCT(\nVALUE\nEND\n')
        self.assertEqual(detect(path), "psf_index")

    def test_vcd_four_state_bus_ranges_aliases_and_same_tick_source_order(self):
        data = self.data()
        bus, alias, digital = data["signals"]
        self.assertEqual([p["value"] for p in bus["points"]], ["0000", "10XZ", "ZZZZ", "1111"])
        self.assertEqual(bus["points"], alias["points"])
        self.assertEqual(bus["range"], (3, 0))
        self.assertEqual(alias["range"], (0, 3))
        self.assertEqual(bus["aliases"], ["tb.alias"])
        self.assertEqual([p["time"] for p in digital["points"]], [0, 5, 5])
        self.assertLess(digital["points"][1]["order"], digital["points"][2]["order"])
        self.assertEqual(bus["coverage"], [0, 20])

    def test_bad_vcd_unit_order_id_width_and_real_are_explicit_errors(self):
        variants = [VCD.replace("$timescale 1 ps $end", ""), VCD.replace("#6", "#4"),
                    VCD.replace("b10xz !", "b10xz ?"), VCD.replace("b10xz !", "b11111 !"),
                    VCD.replace("wire 4 ! bus", "real 4 ! bus")]
        for text in variants:
            with self.subTest(text=text[-80:]), self.assertRaises(BridgeError):
                self.data(text)
        self.error("unsupported_format", self.data, VCD.replace("1 ps", "0.0001fs"))

    def test_budget_is_failure_and_dependency_missing_is_not_empty_data(self):
        path = self.root / "data"
        path.write_text(VCD)
        self.error("budget_exceeded", decode, path, "vcd", {**LIMITS, "points": 2})
        self.error("budget_exceeded", decode, path, "vcd", {**LIMITS, "bytes": 10})
        with patch.dict(sys.modules, {"vcd.reader": None}):
            self.error("dependency_unavailable", decode, path, "vcd", LIMITS)

class WindowSemantics(unittest.TestCase):
    def analog(self, values=((0, 0), (1, 1), (2, 2))):
        return {"name": "a", "kind": "analog", "unit": "V", "time_unit": "s", "coverage": [0, 2],
                "points": [{"time": t, "value": v, "order": i} for i, (t, v) in enumerate(values)]}

    def digital(self, values=((0, "0"), (1, "1"), (2, "0"))):
        value = self.analog(values)
        value.update(name="d", kind="digital", unit="logic", width=1)
        return value

    def test_default_analog_window_excludes_all_right_endpoint_duplicates(self):
        signal = self.analog(((0, 0), (1, 1), (1, 2), (2, 3)))
        self.assertEqual([p["time"] for p in selected_points(signal, [0, 1])], [0])
        self.assertEqual([p["value"] for p in selected_points(signal, [0, 1], include_end=True)], [0, 1, 2])
        self.assertEqual(selected_points(signal), signal["points"])


    def test_window_options_reject_invalid_operations(self):
        raw = {"operation": "read", "artifact": "waves", "analysis": "tran", "signals": ["a"], "window": [0, 1]}
        good = query_spec(raw)
        self.assertFalse(good["include_end"])
        self.assertEqual(query_spec(good), good)
        self.assertTrue(query_spec({**raw, "include_end": True})["include_end"])
        for change in ({"include_end": "true"}, {"include_end": 1}, {"include_end": None},
                       {"operation": "signals", "include_end": True}, {"operation": "sample", "include_end": True}):
            with self.subTest(change=change), self.assertRaises(BridgeError): query_spec({**raw, **change})
        raw.pop("window")
        with self.assertRaises(BridgeError): query_spec({**raw, "include_end": True})

def vcsv(name, points):
    return (';Version, 1, 0\n;' + name + '\n;X, Y\n;Re, Re\n;s, V\n;time, V\n' +
            ''.join(f'{time},{value}\n' for time, value in points)).encode()

class BatchedParse(unittest.TestCase):
    def test_one_process_exact_data_axes_duplicates_window_and_total_budgets(self):
        from ams_bridge import BridgeError
        from ams_bridge.waveforms import parse_local, parse_local_many
        import importlib
        wf = importlib.import_module('ams_bridge.waveforms')
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / f'{i}.vcsv' for i in range(25)]
            for i, path in enumerate(paths):
                path.write_bytes(vcsv(f'top.s{i}', [(0, i), (0, i+1), ((i+1)*1e-9, i+2)]))
            limits = {'points': 75, 'bytes': 1000000, 'timeout_s': 10}
            request = {'operation': 'read', 'signals': [f'top.s{i}' for i in range(25)], 'window': [0, 10e-9]}
            expected = [parse_local(p, 'vcsv', limits, request)['signals'][0] for p in paths]
            actual_run = wf.subprocess.run
            with patch.object(wf.subprocess, 'run', wraps=actual_run) as spawned:
                result = parse_local_many(paths, limits, request)
            self.assertEqual(spawned.call_count, 1)
            self.assertEqual(result['signals'], expected)
            self.assertEqual(result['decoded_points'], 75)
            for cap in [{'points': 74}, {'bytes': sum(p.stat().st_size for p in paths)-1}]:
                with self.assertRaises(BridgeError) as raised:
                    parse_local_many(paths, {**limits, **cap}, request)
                self.assertEqual(raised.exception.code, 'budget_exceeded')
            paths[9].write_bytes(b'not VCSV')
            with self.assertRaisesRegex(BridgeError, '9.vcsv'):
                parse_local_many(paths, limits, request)

class BatchTransfer(unittest.TestCase):
    def setUp(self):
        from ams_bridge.records import metadata
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.handle = {**metadata('job', 'a'*32, 'b'*64), 'remote_directory': '/tmp/v51/'+'a'*32}
        self.original = {'execution': {'ssh_host':'unused','control_timeout_s':20,'transfer_timeout_s':30,'connect_timeout_s':1,'environment':{}}}
        self.names = [f'top.s{i}' for i in range(25)]
        self.query = {'operation':'read','artifact':'analog','analysis':'tran','format':'psfxl','signals':self.names,
                      'limits':{'points':75,'bytes':1000000,'timeout_s':20}}
        self.entry = [{'name':'tran','dataset':'tran-tran','file':'tran.tran.tran','format':'psfxl','analysis_type':'tran'}]
        self.downloads = 0
        self.mode = 'normal'

    def response(self, script, **kwargs):
        from ams_bridge import BridgeError
        response = SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if kwargs.get('output_file') is not None:
            self.downloads += 1
            if self.mode == 'interrupt':
                kwargs['output_file'].write(b'partial tar')
                raise BridgeError('injected transfer interruption', 'communication')
            content = {f'{i}.vcsv':vcsv(name, [(0,i),(0,i+1),(1e-9,i+2)]) for i,name in enumerate(self.names)}
            manifest = ''.join(f'{name}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\n' for name,data in content.items()).encode()
            if self.mode == 'missing':content.pop('9.vcsv')
            if self.mode == 'bad_hash':content['9.vcsv'] = content['9.vcsv'].replace(b';Version', b';Varsion', 1)
            if self.mode == 'unsafe':content['../outside.vcsv'] = b'outside'
            content['manifest.tsv'] = manifest
            with tarfile.open(fileobj=kwargs['output_file'], mode='w') as archive:
                for name,data in content.items():
                    member = tarfile.TarInfo('exports/'+name);member.size=len(data)
                    archive.addfile(member, io.BytesIO(data))
        elif '-print-signal-type' in script:
            response.stdout = ("#dataset 'tran-tran' nbtype 25\n" + ''.join(f"sig[{i:03}] [name:'{name}', simName:'{name}', type:(name:'V', type:srrDouble)]\n" for i,name in enumerate(self.names))).encode()
        elif 'head -c 4096' in script:
            response.stdout = b'IC6.1.8-64b.83'
        return response

    def read(self):
        from ams_bridge.native_psfxl import native_source
        with patch('ams_bridge.transport.SSH.lease',return_value=nullcontext()):
            return native_source(self.handle,self.original,self.directory,self.query,SimpleNamespace(run=self.response),entries=self.entry)

    def test_25_exports_one_download_one_parser_and_warm_is_offline(self):
        import importlib
        from ams_bridge import BridgeError
        from ams_bridge.native_psfxl import native_source
        wf = importlib.import_module('ams_bridge.waveforms');run=wf.subprocess.run
        with patch.object(wf.subprocess,'run',wraps=run) as spawned:
            entries,data,record=self.read()
        self.assertEqual(self.downloads,1);self.assertEqual(spawned.call_count,1)
        self.assertEqual(len(record['exports']),25);self.assertEqual(data['decoded_points'],75)
        with patch('ams_bridge.transport.SSH.run',side_effect=AssertionError('offline')),patch('ams_bridge.transport.SSH.lease',side_effect=AssertionError('offline')),patch.object(wf.subprocess,'run',wraps=run) as spawned:
            _,warm,_=native_source(self.handle,self.original,self.directory,self.query)
        self.assertEqual(spawned.call_count,1);self.assertEqual(warm,data)
        self.query['limits']['points']=74
        with self.assertRaises(BridgeError) as raised:self.read()
        self.assertEqual(raised.exception.code,'budget_exceeded')
        self.assertEqual(self.downloads,1)

    def test_interrupted_missing_and_out_of_budget_batches_not_published(self):
        from ams_bridge import BridgeError
        for mode in ['interrupt','missing','bad_hash','unsafe','budget']:
            self.mode=mode
            if mode=='budget':self.query['limits']['bytes']=100
            with self.assertRaises(BridgeError):self.read()
            self.assertFalse((self.directory/'waveform-cache.json').exists())
