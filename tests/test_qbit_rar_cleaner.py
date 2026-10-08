import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import qbit_rar_cleaner as cleaner


def touch(path: Path, data: bytes = b"rar") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.resolve()


def make_bdmv(release_directory: Path) -> Path:
    bdmv = release_directory / "BDMV"
    touch(bdmv / "index.bdmv", b"index")
    touch(bdmv / "MovieObject.bdmv", b"movie-object")
    (bdmv / "PLAYLIST").mkdir()
    (bdmv / "CLIPINF").mkdir()
    (bdmv / "STREAM").mkdir()
    return bdmv.resolve()


class ScanTests(unittest.TestCase):
    def test_scans_real_modern_and_old_rar_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = touch(root / "Modern" / "movie.part01.rar")
            second = touch(root / "Modern" / "movie.part02.rar")
            old_first = touch(root / "Old" / "release.rar")
            old_second = touch(root / "Old" / "release.r00")
            touch(root / "Old" / "release.nfo")

            result = cleaner.scan_rar_sets(root)

            self.assertEqual(len(result.archive_sets), 2)
            volumes = {item.first_volume: set(item.volumes) for item in result.archive_sets}
            self.assertEqual(volumes[first], {first, second})
            self.assertEqual(volumes[old_first], {old_first, old_second})
            self.assertEqual(result.orphan_volumes, ())

    def test_orphan_continuation_is_not_an_archive_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            orphan = touch(root / "Broken" / "release.r00")

            result = cleaner.scan_rar_sets(root)

            self.assertEqual(result.archive_sets, ())
            self.assertEqual(result.orphan_volumes, (orphan,))

    def test_release_name_does_not_influence_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "Looks.Like.A.RAR.Release").mkdir()

            result = cleaner.scan_rar_sets(root)

            self.assertEqual(result.archive_sets, ())

    def test_iso_scan_is_disk_based_and_skips_internal_and_symlinked_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = touch(root / "Release" / "disc.ISO")
            touch(root / cleaner.WORK_DIR_NAME / "temporary.iso")
            touch(root / cleaner.QUARANTINE_DIR_NAME / "quarantined.iso")
            target = touch(root / "outside.bin")
            (root / "Release" / "linked.iso").symlink_to(target)

            result = cleaner.scan_iso_files(root)

            self.assertEqual(result, (first,))


class FakeReadClient:
    def __init__(self, files_by_hash, trackers_by_hash=None):
        self.files_by_hash = files_by_hash
        self.trackers_by_hash = trackers_by_hash or {}

    def files(self, torrent_hash):
        return self.files_by_hash[torrent_hash]

    def trackers(self, torrent_hash):
        return self.trackers_by_hash.get(torrent_hash, [])


class MatchingTests(unittest.TestCase):
    def test_disk_discovery_is_cross_referenced_by_exact_file_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "Release" / "movie.part01.rar")
            touch(root / "Release" / "movie.part02.rar")
            scan = cleaner.scan_rar_sets(root)
            matching = cleaner.Torrent(
                hash="a" * 40,
                name="Name does not mention archives",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            unrelated = cleaner.Torrent(
                hash="b" * 40,
                name="RAR in the name is irrelevant",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Other",
            )
            client = FakeReadClient(
                {
                    matching.hash: [
                        "Release/movie.part01.rar",
                        "Release/movie.part02.rar",
                    ],
                    unrelated.hash: ["Other/movie.mkv"],
                }
            )

            matched, blocked, _ = cleaner.cross_reference(
                scan, [matching, unrelated], client, root
            )

            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].owners, frozenset({matching.hash}))
            self.assertEqual(blocked, frozenset())

    def test_orphan_volume_blocks_owning_torrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "Broken" / "release.r00")
            scan = cleaner.scan_rar_sets(root)
            torrent = cleaner.Torrent(
                hash="c" * 40,
                name="Broken",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Broken",
            )
            client = FakeReadClient({torrent.hash: ["Broken/release.r00"]})

            matched, blocked, _ = cleaner.cross_reference(scan, [torrent], client, root)

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({torrent.hash}))

    def test_incomplete_cross_seed_protects_shared_rar_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "Release" / "movie.part01.rar")
            touch(root / "Release" / "movie.part02.rar")
            scan = cleaner.scan_rar_sets(root)
            complete = cleaner.Torrent(
                hash="e" * 40,
                name="Complete",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            incomplete = cleaner.Torrent(
                hash="f" * 40,
                name="Incomplete cross-seed",
                state="downloading",
                progress=0.5,
                save_path=root,
                content_path=root / "Release",
            )
            shared_files = [
                "Release/movie.part01.rar",
                "Release/movie.part02.rar",
            ]
            client = FakeReadClient(
                {complete.hash: shared_files, incomplete.hash: shared_files}
            )

            matched, blocked, _ = cleaner.cross_reference(
                scan, [complete, incomplete], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({complete.hash, incomplete.hash}))

    def test_partial_owner_mismatch_blocks_the_whole_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "Release" / "movie.part01.rar")
            touch(root / "Release" / "movie.part02.rar")
            scan = cleaner.scan_rar_sets(root)
            full = cleaner.Torrent(
                hash="1" * 40,
                name="Full owner",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            partial = cleaner.Torrent(
                hash="2" * 40,
                name="Partial owner",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient(
                {
                    full.hash: [
                        "Release/movie.part01.rar",
                        "Release/movie.part02.rar",
                    ],
                    partial.hash: ["Release/movie.part01.rar"],
                }
            )

            matched, blocked, _ = cleaner.cross_reference(
                scan, [full, partial], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({full.hash, partial.hash}))

    def test_aither_tracker_protects_rar_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "Release" / "release.rar")
            scan = cleaner.scan_rar_sets(root)
            torrent = cleaner.Torrent(
                hash="9" * 40,
                name="Protected RAR seed",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient(
                {torrent.hash: ["Release/release.rar"]},
                {torrent.hash: ["https://aither.cc/announce"]},
            )

            matched, blocked, _ = cleaner.cross_reference(
                scan, [torrent], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({torrent.hash}))


class IsoMatchingTests(unittest.TestCase):
    def test_rar_produced_iso_without_torrent_is_still_a_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso")

            matched, blocked, blocked_paths, torrent_by_hash = (
                cleaner.cross_reference_isos([image], [], FakeReadClient({}), root)
            )

            self.assertEqual(matched, (cleaner.IsoImage(image),))
            self.assertEqual(blocked, frozenset())
            self.assertEqual(blocked_paths, ())
            self.assertEqual(torrent_by_hash, {})

    def test_native_iso_is_matched_by_exact_qbittorrent_file_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso")
            torrent = cleaner.Torrent(
                hash="3" * 40,
                name="Native ISO",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient({torrent.hash: ["Release/disc.iso"]})

            matched, blocked, blocked_paths, _ = cleaner.cross_reference_isos(
                [image], [torrent], client, root
            )

            self.assertEqual(matched, (cleaner.IsoImage(image, frozenset({torrent.hash})),))
            self.assertEqual(blocked, frozenset())
            self.assertEqual(blocked_paths, ())

    def test_existing_bdmv_torrent_is_kept_and_only_iso_owner_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = root / "Release"
            image = touch(release / "disc.iso")
            bdmv = make_bdmv(release)
            iso_torrent = cleaner.Torrent(
                hash="a" * 40,
                name="ISO source",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=release / "disc.iso",
            )
            bdmv_torrent = cleaner.Torrent(
                hash="b" * 40,
                name="Extracted BDMV source",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=release,
            )
            client = FakeReadClient(
                {
                    iso_torrent.hash: ["Release/disc.iso"],
                    bdmv_torrent.hash: [
                        "Release/BDMV/index.bdmv",
                        "Release/BDMV/MovieObject.bdmv",
                        "Release/BDMV/PLAYLIST/00000.mpls",
                        "Release/BDMV/CLIPINF/00000.clpi",
                        "Release/BDMV/STREAM/00000.m2ts",
                    ],
                }
            )

            matched, blocked, blocked_paths, _ = cleaner.cross_reference_isos(
                [image], [iso_torrent, bdmv_torrent], client, root
            )
            components = cleaner.build_iso_components(matched)

            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].owners, frozenset({iso_torrent.hash}))
            self.assertEqual(matched[0].existing_bdmv, bdmv)
            self.assertNotIn(bdmv_torrent.hash, matched[0].owners)
            self.assertEqual(components[0].torrent_hashes, frozenset({iso_torrent.hash}))
            self.assertEqual(blocked, frozenset())
            self.assertEqual(blocked_paths, ())

    def test_missing_second_iso_blocks_the_whole_native_torrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc1.iso")
            torrent = cleaner.Torrent(
                hash="4" * 40,
                name="Two discs",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient(
                {torrent.hash: ["Release/disc1.iso", "Release/disc2.iso"]}
            )

            matched, blocked, blocked_paths, _ = cleaner.cross_reference_isos(
                [image], [torrent], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({torrent.hash}))
            self.assertEqual(blocked_paths, (image,))

    def test_aither_tracker_protects_iso_and_torrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso")
            torrent = cleaner.Torrent(
                hash="5" * 40,
                name="Protected seed",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient(
                {torrent.hash: ["Release/disc.iso"]},
                {torrent.hash: ["https://tracker.aither.cc/announce"]},
            )

            matched, blocked, blocked_paths, _ = cleaner.cross_reference_isos(
                [image], [torrent], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({torrent.hash}))
            self.assertEqual(blocked_paths, (image,))

    def test_incomplete_native_iso_torrent_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso")
            torrent = cleaner.Torrent(
                hash="6" * 40,
                name="Downloading",
                state="downloading",
                progress=0.75,
                save_path=root,
                content_path=root / "Release",
            )
            client = FakeReadClient({torrent.hash: ["Release/disc.iso"]})

            matched, blocked, blocked_paths, _ = cleaner.cross_reference_isos(
                [image], [torrent], client, root
            )

            self.assertEqual(matched, ())
            self.assertEqual(blocked, frozenset({torrent.hash}))
            self.assertEqual(blocked_paths, (image,))


class ComponentTests(unittest.TestCase):
    def test_cross_seeded_sets_form_one_component(self):
        root = Path("/storage")
        first = cleaner.ArchiveSet(
            root / "A" / "a.rar",
            (root / "A" / "a.rar",),
            frozenset({"one", "two"}),
        )
        second = cleaner.ArchiveSet(
            root / "A" / "extras.rar",
            (root / "A" / "extras.rar",),
            frozenset({"two"}),
        )

        components = cleaner.build_components([first, second])

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].torrent_hashes, frozenset({"one", "two"}))


class SafetyTests(unittest.TestCase):
    def test_qbittorrent_url_is_strictly_validated_and_normalized(self):
        self.assertEqual(
            cleaner.normalize_qbit_url(" http://127.0.0.1:10987/ "),
            "http://127.0.0.1:10987",
        )
        self.assertEqual(
            cleaner.normalize_qbit_url("http://[::1]:8080"),
            "http://[::1]:8080",
        )
        for invalid in (
            "http://127.0.0.1:10987]",
            "http://127.0.0.1:99999",
            "http://user:secret@127.0.0.1:10987",
            "http://127.0.0.1:10987/api/v2",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(cleaner.CleanerError):
                cleaner.normalize_qbit_url(invalid)

        client = cleaner.QbitClient("http://127.0.0.1:10987", "user", "secret")
        self.assertFalse(client.session.trust_env)

    def test_iso_configuration_does_not_require_a_rar_extractor(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                '{"qb_url":"http://127.0.0.1:8080",'
                '"qb_username":"user","qb_password":"secret",'
                '"scan_root":"/storage","seven_zip":""}',
                encoding="utf-8",
            )

            config = cleaner.load_config(config_path)

            self.assertEqual(config["seven_zip"], "")

    def test_rar_mode_reports_missing_extractor_before_scanning(self):
        with self.assertRaisesRegex(cleaner.CleanerError, "ISO-mód"):
            cleaner.run_rar_mode(None, {}, object(), Path("/"), Path("/"), ())

    def test_prefers_unrar_over_legacy_seven_zip_configuration(self):
        extractor = cleaner.configured_extractor(
            {"unrar": "/usr/bin/unrar", "seven_zip": "/usr/bin/7z"}
        )

        self.assertIsInstance(extractor, cleaner.Unrar)

    def test_unrar_technical_listing_is_parsed_and_links_are_rejected(self):
        listing = """
        Name: movie.mkv
        Type: File
        Size: 44929228245

        Name: BDMV
        Type: Directory
        Size: 0
        """
        extractor = cleaner.Unrar("unrar")
        with mock.patch.object(extractor, "_run") as run:
            run.return_value = mock.Mock(stdout=listing)
            self.assertEqual(
                extractor.entries(Path("release.rar")),
                (
                    cleaner.ArchiveEntry(Path("movie.mkv"), False, 44929228245),
                    cleaner.ArchiveEntry(Path("BDMV"), True, 0),
                ),
            )

            run.return_value = mock.Mock(
                stdout="Name: escape\nType: Unix symbolic link\nSize: 0\nTarget: ../x\n"
            )
            with self.assertRaisesRegex(cleaner.ArchiveError, "linket"):
                extractor.entries(Path("release.rar"))

    def test_rejects_archive_traversal_and_windows_absolute_paths(self):
        unsafe = ["../escape.mkv", "/etc/passwd", "C:\\Windows\\file"]
        for item in unsafe:
            with self.subTest(item=item), self.assertRaises(cleaner.ArchiveError):
                cleaner._safe_archive_member(item)

    def test_qbittorrent_delete_is_hardcoded_list_only(self):
        class RecordingClient(cleaner.QbitClient):
            def __init__(self):
                self.record = None

            def _post_hashes(self, endpoint, hashes, **data):
                self.record = (endpoint, tuple(hashes), data)

        client = RecordingClient()
        client.remove_list_only(["abc"])

        self.assertEqual(
            client.record,
            ("torrents/delete", ("abc",), {"deleteFiles": "false"}),
        )

    def test_different_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage = root / ".stage"
            destination = root / "Release"
            touch(stage / "movie.mkv", b"new")
            touch(destination / "movie.mkv", b"old")

            with self.assertRaises(cleaner.ArchiveError):
                cleaner.prepare_staged_files([(stage, destination)], root)

    def test_tracker_domain_matching_does_not_accept_lookalike_domain(self):
        self.assertTrue(
            cleaner.tracker_url_matches_domain(
                "https://tracker.aither.cc/announce", "aither.cc"
            )
        )
        self.assertFalse(
            cleaner.tracker_url_matches_domain(
                "https://aither.cc.example.org/announce", "aither.cc"
            )
        )
        self.assertFalse(
            cleaner.tracker_url_matches_domain(
                "udp://[2001:db8::1:6969/announce", "aither.cc"
            )
        )

    def test_iso_tree_rejects_symlinks_and_special_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            touch(root / "target.bin")
            (root / "linked.bin").symlink_to(root / "target.bin")
            with self.assertRaises(cleaner.IsoError):
                cleaner.inspect_mounted_tree(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            os.mkfifo(root / "pipe")
            with self.assertRaises(cleaner.IsoError):
                cleaner.inspect_mounted_tree(root)

    def test_identical_existing_iso_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage = root / ".stage"
            destination = root / "Release"
            touch(stage / "BDMV" / "index.bdmv", b"same")
            existing = touch(destination / "BDMV" / "index.bdmv", b"same")

            cleaner.merge_staged_tree([(stage, destination)], root)

            self.assertEqual(existing.read_bytes(), b"same")
            self.assertFalse((stage / "BDMV" / "index.bdmv").exists())

    def test_only_complete_bdmv_structure_uses_delete_only_shortcut(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve() / "Release"
            complete = make_bdmv(release)

            self.assertEqual(cleaner.find_complete_bdmv_structure(release), complete)

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary).resolve() / "Release"
            touch(release / "BDMV" / "index.bdmv")
            (release / "BDMV" / "STREAM").mkdir()

            self.assertIsNone(cleaner.find_complete_bdmv_structure(release))
            complete, replace_paths = cleaner.bdmv_structure_state(release)
            self.assertIsNone(complete)
            self.assertEqual(replace_paths, ((release / "BDMV"),))

    def test_cross_filesystem_move_falls_back_to_atomic_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = touch(root / "source.bin", b"payload")
            destination = root / "destination.bin"
            real_replace = os.replace

            def selective_replace(first, second):
                if Path(first) == source:
                    raise OSError(cleaner.errno.EXDEV, "Invalid cross-device link")
                return real_replace(first, second)

            with mock.patch.object(cleaner.os, "replace", side_effect=selective_replace):
                cleaner.move_file_cross_device(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"payload")


class FakeExtractor:
    def test(self, first_volume):
        return None

    def entries(self, first_volume):
        return (cleaner.ArchiveEntry(Path("movie.mkv"), False, 5),)

    def extract(self, first_volume, destination):
        destination.mkdir(parents=True)
        (destination / "movie.mkv").write_bytes(b"movie")


class FakeMutationClient:
    def __init__(self, torrent):
        self.torrent = torrent
        self.stopped = False
        self.removed = False
        self.started = False

    def torrents(self, hashes=None):
        if self.removed:
            return []
        state = "stoppedUP" if self.stopped else self.torrent.state
        return [cleaner.replace(self.torrent, state=state)]

    def stop(self, hashes):
        self.stopped = True

    def start(self, hashes):
        self.started = True
        self.stopped = False

    def remove_list_only(self, hashes):
        self.removed = True


class FakeIsoMounter:
    def __init__(self, content_by_name, events, fail_unmount=False):
        self.content_by_name = content_by_name
        self.events = events
        self.fail_unmount = fail_unmount

    def mount(self, image, mount_point):
        self.events.append(f"mount:{image.name}")
        mount_point.mkdir(parents=True)
        for relative, data in self.content_by_name[image.name].items():
            touch(mount_point / relative, data)

    def unmount(self, mount_point):
        self.events.append(f"unmount:{mount_point.name}")
        for child in list(mount_point.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        if self.fail_unmount:
            raise cleaner.IsoUnmountError("simulated unmount failure")


class FakeIsoMutationClient:
    def __init__(self, torrent, events):
        self.torrent = torrent
        self.events = events
        self.stopped = False
        self.removed = False
        self.started = False

    def torrents(self, hashes=None):
        if self.torrent is None or self.removed:
            return []
        state = "stoppedUP" if self.stopped else self.torrent.state
        return [cleaner.replace(self.torrent, state=state)]

    def stop(self, hashes):
        self.events.append("stop")
        self.stopped = True

    def start(self, hashes):
        self.events.append("start")
        self.started = True
        self.stopped = False

    def remove_list_only(self, hashes):
        self.events.append("remove-list-only")
        self.removed = True


class TransactionTests(unittest.TestCase):
    def test_success_extracts_then_script_deletes_rar_and_removes_list_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = touch(root / "Release" / "release.rar", b"archive")
            torrent = cleaner.Torrent(
                hash="d" * 40,
                name="Release",
                state="stalledUP",
                progress=1,
                save_path=root,
                content_path=root / "Release",
            )
            archive_set = cleaner.ArchiveSet(
                first, (first,), frozenset({torrent.hash})
            )
            component = cleaner.Component((archive_set,), frozenset({torrent.hash}))
            client = FakeMutationClient(torrent)

            success, message = cleaner.process_component(
                component,
                {torrent.hash: torrent},
                client,
                FakeExtractor(),
                root,
                root / cleaner.WORK_DIR_NAME,
                0,
            )

            self.assertTrue(success, message)
            self.assertFalse(first.exists())
            self.assertEqual((root / "Release" / "movie.mkv").read_bytes(), b"movie")
            self.assertTrue(client.removed)


class IsoTransactionTests(unittest.TestCase):
    def make_torrent(self, root):
        return cleaner.Torrent(
            hash="7" * 40,
            name="Native ISO release",
            state="stalledUP",
            progress=1,
            save_path=root,
            content_path=root / "Release",
        )

    def test_native_iso_unmounts_then_deletes_iso_and_removes_list_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso", b"iso")
            torrent = self.make_torrent(root)
            component = cleaner.IsoComponent(
                (cleaner.IsoImage(image, frozenset({torrent.hash})),),
                frozenset({torrent.hash}),
            )
            events = []
            mounter = FakeIsoMounter(
                {"disc.iso": {"BDMV/index.bdmv": b"index"}}, events
            )
            client = FakeIsoMutationClient(torrent, events)

            success, message = cleaner.process_iso_component(
                component,
                {torrent.hash: torrent},
                client,
                mounter,
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertTrue(success, message)
            self.assertFalse(image.exists())
            self.assertEqual(
                (root / "Release" / "BDMV" / "index.bdmv").read_bytes(),
                b"index",
            )
            self.assertTrue(client.removed)
            self.assertLess(events.index("unmount:0"), events.index("stop"))
            self.assertLess(events.index("stop"), events.index("remove-list-only"))

    def test_rar_produced_ownerless_iso_is_deleted_without_qbit_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "from-rar.iso", b"iso")
            component = cleaner.IsoComponent(
                (cleaner.IsoImage(image),), frozenset()
            )
            events = []
            mounter = FakeIsoMounter(
                {"from-rar.iso": {"VIDEO_TS/VIDEO_TS.IFO": b"dvd"}}, events
            )
            client = FakeIsoMutationClient(None, events)

            success, message = cleaner.process_iso_component(
                component,
                {},
                client,
                mounter,
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertTrue(success, message)
            self.assertFalse(image.exists())
            self.assertEqual(
                (root / "Release" / "VIDEO_TS" / "VIDEO_TS.IFO").read_bytes(),
                b"dvd",
            )
            self.assertNotIn("stop", events)
            self.assertNotIn("remove-list-only", events)

    def test_existing_complete_bdmv_deletes_iso_without_mounting_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = root / "Release"
            image = touch(release / "disc.iso", b"iso")
            bdmv = make_bdmv(release)
            torrent = self.make_torrent(root)
            component = cleaner.IsoComponent(
                (
                    cleaner.IsoImage(
                        image, frozenset({torrent.hash}), existing_bdmv=bdmv
                    ),
                ),
                frozenset({torrent.hash}),
            )
            events = []
            mounter = FakeIsoMounter({}, events)
            client = FakeIsoMutationClient(torrent, events)

            success, message = cleaner.process_iso_component(
                component,
                {torrent.hash: torrent},
                client,
                mounter,
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertTrue(success, message)
            self.assertFalse(image.exists())
            self.assertTrue((bdmv / "index.bdmv").exists())
            self.assertFalse(any(event.startswith("mount:") for event in events))
            self.assertTrue(client.removed)

    def test_partial_bdmv_is_replaced_from_iso_after_successful_unmount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = root / "Release"
            image = touch(release / "disc.iso", b"iso")
            old_marker = touch(release / "BDMV" / "old-partial.bin", b"old")
            component = cleaner.IsoComponent(
                (
                    cleaner.IsoImage(
                        image, replace_bdmv=(release / "BDMV",)
                    ),
                ),
                frozenset(),
            )
            events = []
            mounter = FakeIsoMounter(
                {
                    "disc.iso": {
                        "BDMV/index.bdmv": b"index",
                        "BDMV/MovieObject.bdmv": b"movie-object",
                        "BDMV/PLAYLIST/00000.mpls": b"playlist",
                        "BDMV/CLIPINF/00000.clpi": b"clip",
                        "BDMV/STREAM/00000.m2ts": b"stream",
                    }
                },
                events,
            )
            client = FakeIsoMutationClient(None, events)

            success, message = cleaner.process_iso_component(
                component,
                {},
                client,
                mounter,
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertTrue(success, message)
            self.assertFalse(image.exists())
            self.assertFalse(old_marker.exists())
            self.assertEqual(
                (release / "BDMV" / "STREAM" / "00000.m2ts").read_bytes(),
                b"stream",
            )
            self.assertIn("unmount:0", events)

    def test_partial_bdmv_remains_when_iso_has_no_complete_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = root / "Release"
            image = touch(release / "disc.iso", b"iso")
            old_marker = touch(release / "BDMV" / "old-partial.bin", b"old")
            component = cleaner.IsoComponent(
                (
                    cleaner.IsoImage(
                        image, replace_bdmv=(release / "BDMV",)
                    ),
                ),
                frozenset(),
            )
            events = []
            client = FakeIsoMutationClient(None, events)

            success, _ = cleaner.process_iso_component(
                component,
                {},
                client,
                FakeIsoMounter({"disc.iso": {"not-bdmv.bin": b"data"}}, events),
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertFalse(success)
            self.assertTrue(image.exists())
            self.assertEqual(old_marker.read_bytes(), b"old")
            self.assertIn("unmount:0", events)

    def test_partial_bdmv_is_restored_when_output_merge_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release = root / "Release"
            image = touch(release / "disc.iso", b"iso")
            old_marker = touch(release / "BDMV" / "old-partial.bin", b"old")
            existing = touch(release / "existing.txt", b"old-existing")
            component = cleaner.IsoComponent(
                (
                    cleaner.IsoImage(
                        image, replace_bdmv=(release / "BDMV",)
                    ),
                ),
                frozenset(),
            )
            events = []
            content = {
                "BDMV/index.bdmv": b"index",
                "BDMV/MovieObject.bdmv": b"movie-object",
                "BDMV/PLAYLIST/00000.mpls": b"playlist",
                "BDMV/CLIPINF/00000.clpi": b"clip",
                "BDMV/STREAM/00000.m2ts": b"stream",
                "existing.txt": b"different",
            }

            success, _ = cleaner.process_iso_component(
                component,
                {},
                FakeIsoMutationClient(None, events),
                FakeIsoMounter({"disc.iso": content}, events),
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertFalse(success)
            self.assertTrue(image.exists())
            self.assertEqual(old_marker.read_bytes(), b"old")
            self.assertEqual(existing.read_bytes(), b"old-existing")

    def test_different_existing_iso_output_keeps_iso_and_torrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso", b"iso")
            existing = touch(root / "Release" / "existing.txt", b"old")
            torrent = self.make_torrent(root)
            component = cleaner.IsoComponent(
                (cleaner.IsoImage(image, frozenset({torrent.hash})),),
                frozenset({torrent.hash}),
            )
            events = []
            mounter = FakeIsoMounter(
                {"disc.iso": {"existing.txt": b"new"}}, events
            )
            client = FakeIsoMutationClient(torrent, events)

            success, _ = cleaner.process_iso_component(
                component,
                {torrent.hash: torrent},
                client,
                mounter,
                root,
                root / "work",
                0,
                (os.getuid(), os.getgid()),
            )

            self.assertFalse(success)
            self.assertTrue(image.exists())
            self.assertEqual(existing.read_bytes(), b"old")
            self.assertFalse(client.removed)
            self.assertNotIn("stop", events)

    def test_unmount_failure_is_fatal_and_keeps_iso(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = touch(root / "Release" / "disc.iso", b"iso")
            component = cleaner.IsoComponent(
                (cleaner.IsoImage(image),), frozenset()
            )
            events = []
            mounter = FakeIsoMounter(
                {"disc.iso": {"file.bin": b"data"}}, events, fail_unmount=True
            )
            client = FakeIsoMutationClient(None, events)

            with self.assertRaises(cleaner.IsoUnmountError):
                cleaner.process_iso_component(
                    component,
                    {},
                    client,
                    mounter,
                    root,
                    root / "work",
                    0,
                    (os.getuid(), os.getgid()),
                )

            self.assertTrue(image.exists())
            self.assertNotIn("remove-list-only", events)


if __name__ == "__main__":
    unittest.main()
