import hashlib
from pathlib import Path

import pytest

from xian_cli.contract_bundles import read_contract_source_from_bundle


def test_source_read_returns_the_exact_bytes_that_were_verified(tmp_path, monkeypatch):
    source = tmp_path / "contract.py"
    original = b"# original\r\n"
    source.write_bytes(original)
    entry = {"path": source.name, "sha256": hashlib.sha256(original).hexdigest()}
    read_bytes = Path.read_bytes
    reads = []

    def read_then_change(path):
        data = read_bytes(path)
        reads.append(path)
        path.write_bytes(b"# modified\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_change)
    assert read_contract_source_from_bundle(tmp_path / "bundle.json", entry) == original.decode()
    assert reads == [source]
    with pytest.raises(ValueError, match="sha256 mismatch"):
        read_contract_source_from_bundle(tmp_path / "bundle.json", entry)
