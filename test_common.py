import os
import pathlib
import pytest

import common
import host


def _read_file(filename: str) -> str:
    with open(filename) as f:
        return f.read()


def test_atomic_write(tmp_path: pathlib.Path) -> None:

    user = os.geteuid()
    group = os.getegid()

    filename = str(tmp_path / "file1")
    with common.atomic_write(filename) as f:
        f.write("hello1")
        f.flush()
        d = os.listdir(str(tmp_path))
        assert len(d) == 1
        (filename_tmp,) = d
        assert filename_tmp.startswith("file1.")
        filename_tmp = str(tmp_path / filename_tmp)
        assert _read_file(filename_tmp) == "hello1"
        assert not os.path.exists(filename)

        st = os.stat(filename_tmp)
        assert st.st_mode == 0o100600
        assert st.st_uid == user

    assert os.path.exists(filename)
    assert not os.path.exists(filename_tmp)
    assert _read_file(filename) == "hello1"
    st = os.stat(filename)
    assert st.st_mode == 0o100644

    with common.atomic_write(filename) as f:
        f.write("hello1.2")
        f.flush()
        d = os.listdir(str(tmp_path))
        assert len(d) == 2
        assert "file1" in d
        d.remove("file1")
        (filename_tmp,) = d
        assert filename_tmp.startswith("file1.")
        assert _read_file(str(tmp_path / filename_tmp)) == "hello1.2"
        assert os.path.exists(filename)
        assert _read_file(filename) == "hello1"

    assert os.path.exists(filename)
    assert not os.path.exists(filename_tmp)
    assert _read_file(filename) == "hello1.2"

    filename = str(tmp_path / "file2")
    with common.atomic_write(filename, mode=0o002) as f:
        f.write("hello2")
    assert os.path.exists(filename)
    if os.access(filename, os.R_OK):
        # If we have CAP_DAC_OVERRIDE, we still can open the file despite the
        # file permissions.
        open(filename)
    else:
        with pytest.raises(PermissionError):
            # We took away permissions to read the file.
            open(filename)
    st = os.stat(filename)
    assert st.st_mode == 0o100002

    filename = str(tmp_path / "file3")
    with common.atomic_write(filename, owner=user, group=group, mode=0o644) as f:
        f.write("hello3")
    assert _read_file(filename) == "hello3"


def test_ip_address() -> None:
    with pytest.raises(TypeError):
        common.ipaddr_norm(None)  # type: ignore
    assert common.ipaddr_norm("") is None
    assert common.ipaddr_norm(b"\xc8") is None
    assert common.ipaddr_norm(" 1.2.3.8  ") == "1.2.3.8"
    assert common.ipaddr_norm(b" 1.2.3.8  ") == "1.2.3.8"
    assert common.ipaddr_norm(" 1::01  ") == "1::1"
    assert common.ipaddr_norm(b" 1::01  ") == "1::1"
    assert common.ipaddr_norm(b" 1::01  ") == "1::1"


def test_ip_addrs() -> None:
    # We expect to have at least one address configured on the system and that
    # `ip -json addr` works. The unit test requires that.
    assert common.ip_addrs(host.LocalHost())


def test_ip_links() -> None:
    links = common.ip_links(host.LocalHost())
    assert links
    assert [link.ifindex for link in links if link.ifname == "lo"] == [1]

    assert [link.ifindex for link in common.ip_links(host.LocalHost(), ifname="lo")] == [1]


def test_ip_routes() -> None:
    # We expect to have at least one route configured on the system and that
    # `ip -json route` works. The unit test requires that.
    assert common.ip_routes(host.LocalHost())


def test_rangelist() -> None:
    RangeList = common.RangeList

    rl = RangeList(include=RangeList.parse_list(["1", 5, "7-9"]))
    include_lst = [1, 5, 7, 8, 9]
    assert rl._include == set(include_lst)
    assert rl._exclude is None
    assert rl.filter(str(i) for i in range(20)) == [str(i) for i in include_lst]

    rl = RangeList(exclude=RangeList.parse_list([5, 5, 7, "1-2,9"]))
    exclude_lst = [1, 2, 5, 7, 9]
    assert rl._include is None
    assert rl._exclude == set(exclude_lst)
    assert rl.filter(str(i) for i in range(20)) == [str(i) for i in range(20) if i not in exclude_lst]

    rl = RangeList(
        include=RangeList.parse_list(["1", 5, "7-9", [13, 14, 15]]),
        exclude=RangeList.parse_list("7-8"),
    )
    include_lst = [1, 5, 7, 8, 9, 13, 14, 15]
    exclude_lst = [7, 8]
    combined_lst = [1, 5, 9, 13, 14, 15]
    assert rl._include == set(include_lst)
    assert rl._exclude == set(exclude_lst)
    assert rl.filter(str(i) for i in range(20)) == [str(i) for i in combined_lst]

    rl = RangeList()
    assert (rl._include, rl._exclude) == (None, None)
    rl._accumulate(True, "1-4")
    assert (rl._include, rl._exclude) == ({1, 2, 3, 4}, None)
    rl._accumulate(False, "3")
    assert (rl._include, rl._exclude) == ({1, 2, 4}, {3})
