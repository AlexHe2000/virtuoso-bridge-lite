"""Check built distributions, not just imports from an editable checkout."""

from importlib.metadata import version
import os
from pathlib import Path
import tarfile
import zipfile

import pytest


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_distribution_preserves_package_files(artifact: str) -> None:
    configured = os.environ.get("VB_TEST_DIST_DIR")
    if not configured:
        pytest.skip("set VB_TEST_DIST_DIR after uv build to check distributions")
    dist = Path(configured)
    package_version = version("virtuoso-bridge")
    source = Path(__file__).resolve().parents[1] / "src"
    expected = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in (source / "virtuoso_bridge").rglob("*")
        if path.is_file()
        and (path.suffix in {".py", ".il", ".yaml"} or path.name == ".env_template")
    }
    assert "virtuoso_bridge/virtuoso/schematic/cdf_param_filters.yaml" in expected
    if artifact == "wheel":
        with zipfile.ZipFile(dist / f"virtuoso_bridge-{package_version}-py3-none-any.whl") as archive:
            names = set(archive.namelist())
            for name, content in expected.items():
                assert name in names, f"wheel missing package file: {name}"
                assert archive.read(name) == content, f"wheel changed package file: {name}"
    else:
        with tarfile.open(dist / f"virtuoso_bridge-{package_version}.tar.gz", "r:gz") as archive:
            names = set(archive.getnames())
            for name, content in expected.items():
                member = f"virtuoso_bridge-{package_version}/src/{name}"
                assert member in names, f"sdist missing package file: {name}"
                stream = archive.extractfile(member)
                assert stream is not None
                with stream:
                    assert stream.read() == content, f"sdist changed package file: {name}"
