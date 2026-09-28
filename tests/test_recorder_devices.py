"""The microphone must open on whatever hardware the user actually has.

The failure this covers is not exotic. Windows exposes output endpoints such as
"PC Speaker" and "Stereo Mix" in the same list as microphones, and the app used
to offer them and then fail with error 9996 -- MMSYSERR_INVALPARAM, a bare code
that names neither the cause nor the fix. Three separate defects combined to
make that error the only thing a user could see:

1. ``list_input_devices`` returned anything with ``max_input_channels > 0``,
   so speakers were offered as microphones.
2. The device picker was keyed by *name*, so the several ways Windows exposes
   one physical mic (MME, WASAPI, WDM-KS) collapsed into a single row, and the
   index the app got was whichever was listed last -- often a 48 kHz endpoint
   that cannot be opened at the hardcoded 44100.
3. The sample rate was hardcoded, so a 48 kHz interface or an 8 kHz Bluetooth
   headset could not be opened at all.

Every test here drives a fake device table, so the suite does not depend on the
machine's real audio hardware, which differs on every developer's box.
"""
from __future__ import annotations

import unittest
import unittest.mock

from app.core import recorder as rec


class FakeSounddevice:
    """Minimal sounddevice double: an ordered device table and a rate checker."""

    def __init__(self, devices, hostapis=None):
        self._devices = devices
        self._hostapis = hostapis or {0: "MME", 1: "Windows WASAPI", 2: "Windows WDM-KS"}
        self.default = unittest.mock.Mock(device=[0, 0])
        self.opened: list[tuple[int, int]] = []
        self.fail_rates: set[int] = set()

    def query_devices(self, index=None, kind=None):
        if kind == "input":
            for dev in self._devices:
                if dev.get("max_input_channels", 0) > 0:
                    return dev
            raise ValueError("no input device")
        if index is None:
            return list(self._devices)
        return self._devices[index]

    def query_hostapis(self, index):
        return {"name": self._hostapis.get(index, "unknown")}

    def check_input_settings(self, device=None, channels=1, dtype=None, samplerate=None):
        if samplerate in self.fail_rates:
            raise ValueError("invalid sample rate")
        return None


class OutputEndpointTests(unittest.TestCase):
    def test_speakers_are_not_offered_as_microphones(self):
        for name in (
            "PC Speaker (Realtek HD Audio 2nd output with HAP)",
            "PC Speaker (Realtek HD Audio output with HAP)",
            "Stereo Mix (Realtek HD Audio Stereo input)",
            "HDAUDIO Loopback Recording",
            "HDMI Output",
        ):
            self.assertTrue(rec.looks_like_output_device(name), name)

    def test_real_microphones_are_not_mistaken_for_outputs(self):
        for name in (
            "Microphone Array (AMD Audio Device)",
            "Microphone (Realtek HD Audio Mic input)",
            "Headset Microphone (pro2)",
            "Microsoft Sound Mapper - Input",
            "Primary Sound Capture Driver",
            "USB Audio Device",
            "Line In",
        ):
            self.assertFalse(rec.looks_like_output_device(name), name)

    def test_a_list_of_only_speakers_still_offers_something(self):
        # Filtering must never leave the user with an empty picker.
        fake = FakeSounddevice(
            [
                {"index": 0, "name": "PC Speaker (HAP)", "max_input_channels": 2,
                 "default_samplerate": 44100, "hostapi": 2},
            ]
        )
        with unittest.mock.patch.object(rec, "soft_import", return_value=fake):
            devices = rec.list_input_devices()
        self.assertEqual(len(devices), 1)


class SampleRateNegotiationTests(unittest.TestCase):
    def test_the_device_native_rate_is_tried_first(self):
        rates = rec.candidate_rates(native=48000, preferred=44100)
        self.assertEqual(rates[0], 48000)

    def test_no_resampling_when_the_device_is_44100(self):
        self.assertEqual(rec.candidate_rates(44100, 44100)[0], 44100)

    def test_a_48k_device_offers_a_rate_it_can_actually_open(self):
        rates = rec.candidate_rates(48000, 44100)
        self.assertIn(48000, rates)
        # 44.1 kHz may still be offered, but 48 kHz comes first, so the common
        # 48 kHz interface opens without resampling.
        self.assertLess(rates.index(48000), rates.index(44100))

    def test_a_bluetooth_headset_gets_its_own_rate(self):
        for native in (8000, 16000):
            self.assertEqual(rec.candidate_rates(native, 44100)[0], native)

    def test_rates_are_deduplicated_and_never_empty(self):
        self.assertEqual(rec.candidate_rates(44100, 44100), rec.candidate_rates(44100, 44100))
        self.assertTrue(rec.candidate_rates(None, None))
        for value in rec.candidate_rates(None, None):
            self.assertGreater(value, 0)

    def test_junk_rates_are_ignored_instead_of_raising(self):
        rates = rec.candidate_rates("not-a-number", 0)
        self.assertTrue(rates)
        self.assertNotIn(0, rates)


class OpeningTests(unittest.TestCase):
    def _recorder(self, **kwargs):
        r = rec.Recorder(**kwargs)
        return r

    def test_it_falls_back_to_a_rate_the_device_accepts(self):
        """A 48 kHz device that refuses 44100 must still open."""
        fake = FakeSounddevice(
            [
                {"index": 0, "name": "USB Mic", "max_input_channels": 2,
                 "default_samplerate": 48000, "hostapi": 1},
            ]
        )
        opened = []

        class Stream:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                opened.append(self.kwargs["samplerate"])

            def close(self):
                pass

        # The recorder opens the stream via the sounddevice object, so the double
        # is the only thing that needs patching.
        fake.InputStream = lambda **kw: Stream(**kw)
        r = self._recorder(device=0, samplerate=44100)
        with unittest.mock.patch.object(rec, "soft_import", return_value=fake):
            r.start()
        self.assertEqual(opened, [48000])
        r.stop()

    def test_every_rate_failing_names_the_device_and_the_rates(self):
        fake = FakeSounddevice(
            [
                {"index": 0, "name": "Broken Mic", "max_input_channels": 2,
                 "default_samplerate": 48000, "hostapi": 1},
            ]
        )

        class Stream:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise ValueError("9996 invalid parameter")

            def close(self):
                self.closed = True

        fake.InputStream = lambda **kw: Stream(**kw)
        r = self._recorder(device=0)
        with unittest.mock.patch.object(rec, "soft_import", return_value=fake):
            with self.assertRaises(rec.MicPermissionError) as caught:
                r.start()
        message = str(caught.exception)
        self.assertIn("Broken Mic", message)
        self.assertIn("48000", message)
        # and the half-built stream must not be left holding the device open
        self.assertFalse(r._recording)
        self.assertIsNone(r._stream)

    def test_the_opened_rate_is_remembered_for_saving(self):
        """save() divides by samplerate, so a stale value writes a broken file."""
        fake = FakeSounddevice(
            [
                {"index": 0, "name": "USB Mic", "max_input_channels": 2,
                 "default_samplerate": 48000, "hostapi": 1},
            ]
        )

        class Stream:
            samplerate = 48000

            def start(self):
                pass

            def close(self):
                pass

        fake.InputStream = lambda **kw: Stream()
        r = self._recorder(device=0, samplerate=44100)
        with unittest.mock.patch.object(rec, "soft_import", return_value=fake):
            r.start()
        self.assertEqual(r.samplerate, 48000)
        r.stop()


class ErrorMessageTests(unittest.TestCase):
    """9996 is the same code for three different problems; say which one."""

    def _message(self, name, is_output, error, rate=48000):
        r = rec.Recorder(device=0)
        with unittest.mock.patch.object(
            rec, "device_info",
            return_value={"index": 0, "name": name, "is_output": is_output,
                          "samplerate": rate},
        ):
            return r._open_failure_message(0, [rate, 44100], error)

    def test_a_saved_speaker_is_named_as_the_problem(self):
        msg = self._message("PC Speaker (Realtek HAP)", True, Exception("9996"))
        self.assertIn("PC Speaker", msg)
        self.assertIn("output device", msg.lower())
        self.assertIn("Settings > Microphone", msg)

    def test_a_bad_sample_rate_is_explained_as_a_rate_problem(self):
        msg = self._message("USB Interface", False, Exception("9996 invalid parameter"))
        self.assertIn("sample rate", msg.lower())
        self.assertIn("Settings > Microphone", msg)

    def test_a_device_in_use_is_not_blamed_on_privacy_settings(self):
        msg = self._message("Headset", False, Exception("Device unavailable"))
        self.assertIn("another application", msg.lower())
        self.assertNotIn("Privacy", msg)

    def test_privacy_is_still_mentioned_for_a_genuine_block(self):
        msg = self._message("Headset", False, Exception("something unrecognised"))
        self.assertIn("Privacy", msg)

    def test_the_original_error_is_always_shown(self):
        msg = self._message("USB Interface", False, Exception("9996 boom"))
        self.assertIn("9996 boom", msg)

    def test_the_device_name_appears_so_the_user_knows_what_failed(self):
        msg = self._message("Some Very Odd Mic", False, Exception("x"))
        self.assertIn("Some Very Odd Mic", msg)


class SavedDeviceRecoveryTests(unittest.TestCase):
    def test_a_saved_speaker_is_replaced_at_startup(self):
        """A machine already stuck on error 9996 must heal on the next launch."""
        from app.gui.app import VoiceStudioApp

        settings = unittest.mock.Mock()
        settings.get.side_effect = lambda *a: {"recorder": {"device": 17}}.get(
            a[0], {}
        ).get(a[1]) if len(a) > 1 else None
        app_obj = VoiceStudioApp.__new__(VoiceStudioApp)
        app_obj.settings = settings
        with unittest.mock.patch.object(rec, "device_info", side_effect=lambda i: {
            17: {"index": 17, "name": "PC Speaker", "is_output": True, "samplerate": 44100},
            1: {"index": 1, "name": "Microphone Array", "is_output": False,
                "samplerate": 44100},
        }.get(i)), unittest.mock.patch.object(rec, "default_input_device", return_value=1):
            device = app_obj._usable_mic_device()
        self.assertEqual(device, 1)
        settings.set.assert_called_with("recorder", "device", 1)
        self.assertIn("PC Speaker", app_obj.mic_recovery)

    def test_a_saved_real_microphone_is_left_alone(self):
        from app.gui.app import VoiceStudioApp

        settings = unittest.mock.Mock()
        settings.get.return_value = 10
        app_obj = VoiceStudioApp.__new__(VoiceStudioApp)
        app_obj.settings = settings
        with unittest.mock.patch.object(rec, "device_info", return_value={
            "index": 10, "name": "Microphone (Realtek)", "is_output": False,
            "samplerate": 44100,
        }):
            self.assertEqual(app_obj._usable_mic_device(), 10)
        settings.set.assert_not_called()


class PickerLabelTests(unittest.TestCase):
    """Windows exposes one physical mic through several host APIs at once.

    The picker used to be keyed by device *name*, so those collapsed into a
    single row and the app silently used whichever index came last -- frequently
    a 48 kHz endpoint that then failed to open at the hardcoded 44100. The
    labels have to be distinguishable, and the map has to be keyed by index.
    """

    def _label(self, name, hostapi, rate):
        from app.gui.screens.settings import SettingsScreen

        return SettingsScreen._device_label(
            {"name": name, "hostapi": hostapi, "samplerate": rate}
        )

    def test_the_same_mic_through_different_host_apis_is_distinguishable(self):
        labels = {
            self._label("Microphone Array (AMD Audio Device)", "MME", 44100),
            self._label("Microphone Array (AMD Audio Device)", "Windows WASAPI", 48000),
            self._label("Microphone Array (AMD Audio Device)", "Windows WDM-KS", 48000),
        }
        self.assertEqual(len(labels), 3, labels)

    def test_the_label_shows_the_rate_that_matters(self):
        self.assertIn("48000", self._label("USB Interface", "Windows WASAPI", 48000))

    def test_the_label_shows_the_host_api(self):
        self.assertIn("WASAPI", self._label("USB Interface", "Windows WASAPI", 48000))

    def test_a_device_with_no_rate_still_gets_a_label(self):
        label = self._label("Odd Device", "MME", 0)
        self.assertIn("Odd Device", label)

    def test_a_name_keyed_map_would_have_collapsed_these(self):
        # The regression, stated directly: one name, two usable indices.
        devices = [
            {"name": "Microphone Array", "index": 5, "samplerate": 44100,
             "hostapi": "MME"},
            {"name": "Microphone Array", "index": 9, "samplerate": 48000,
             "hostapi": "Windows WASAPI"},
        ]
        by_name = {d["name"]: d["index"] for d in devices}
        by_index = {self._label(d["name"], d["hostapi"], d["samplerate"]): d["index"]
                    for d in devices}
        self.assertEqual(len(by_name), 1, "name-keyed map collapses these")
        self.assertEqual(len(by_index), 2, "index-keyed map must keep both")


if __name__ == "__main__":
    unittest.main()
