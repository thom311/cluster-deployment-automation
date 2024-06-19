import dataclasses
import os
import pathlib
import pytest
import typing


import common
import host


def _read_file(filename: str) -> str:
    with open(filename) as f:
        return f.read()


def test_seconds_to_str() -> None:
    def _t(val: float, *, negative_test: bool = False) -> str:
        if negative_test:
            assert val < 0.0
        else:
            assert val >= 0.0
        res = common.seconds_to_str(float(val))
        if val == int(val):
            assert res == common.seconds_to_str(int(val))

        if val > 0.0:
            assert f"-{res}" == _t(float(-val), negative_test=True)

        return res

    def _t2(d: int, h: int, m: int, s: float) -> str:
        return _t((((((d * 24) + h) * 60) + m) * 60) + s)

    assert _t(0) == "0s"
    assert _t(0.4) == "0.4s"

    assert _t(0.000001) == "0.001s"

    assert _t(0) == "0s"
    assert _t(0.9994) == "0.999s"
    assert _t(0.9996) == "1s"
    assert _t(0.999999) == "1s"
    assert _t(1) == "1s"

    assert _t(59) == "59s"
    assert _t(59.9994) == "59.999s"
    assert _t(59.9996) == "1m"
    assert _t(59.999999) == "1m"
    assert _t(60) == "1m"

    assert _t2(0, 0, 5, 0) == "5m"
    assert _t2(0, 0, 5, 1) == "5m 1s"
    assert _t2(0, 0, 5, 1.1) == "5m 1.1s"

    assert _t2(4, 0, 0, 0) == "4d"
    assert _t2(4, 21, 0, 0) == "4d 21h"
    assert _t2(4, 0, 3, 0) == "4d 0h 3m"
    assert _t2(4, 0, 3, 44) == "4d 0h 3m 44s"
    assert _t2(4, 18, 55, 0.43) == "4d 18h 55m 0.43s"

    assert _t2(4, 23, 59, 59.9994) == "4d 23h 59m 59.999s"
    assert _t2(4, 23, 59, 59.9996) == "5d"

    assert _t2(5, 0, 0, 0) == "5d"
    assert _t2(5, 0, 0, 0.1) == "5d 0h 0m 0.1s"
    assert _t2(5, 0, 59, 59.9994) == "5d 0h 59m 59.999s"
    assert _t2(5, 0, 59, 59.9996) == "5d 1h"

    assert _t2(50000000000, 0, 59, 59.9996) == "50000000000d 1h"


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


def test_strict_dataclass() -> None:
    @common.strict_dataclass
    @dataclasses.dataclass
    class C2:
        a: str
        b: int
        c: typing.Optional[str] = None

    C2("a", 5)
    C2("a", 5, None)
    C2("a", 5, "")
    with pytest.raises(TypeError):
        C2("a", "5")  # type: ignore
    with pytest.raises(TypeError):
        C2(3, 5)  # type: ignore
    with pytest.raises(TypeError):
        C2("a", 5, [])  # type: ignore

    @common.strict_dataclass
    @dataclasses.dataclass
    class C3:
        a: typing.List[str]

    C3([])
    C3([""])
    with pytest.raises(TypeError):
        C3(1)  # type: ignore
    with pytest.raises(TypeError):
        C3([1])  # type: ignore
    with pytest.raises(TypeError):
        C3(None)  # type: ignore

    @common.strict_dataclass
    @dataclasses.dataclass
    class C4:
        a: typing.Optional[typing.List[str]]

    C4(None)

    @common.strict_dataclass
    @dataclasses.dataclass
    class C5:
        a: typing.Optional[typing.List[typing.Dict[str, str]]] = None

    C5(None)
    C5([])
    with pytest.raises(TypeError):
        C5([1])  # type: ignore
    C5([{}])
    C5([{"a": "b"}])
    C5([{"a": "b"}, {}])
    C5([{"a": "b"}, {"c": "", "d": "x"}])
    with pytest.raises(TypeError):
        C5([{"a": None}])  # type: ignore

    @common.strict_dataclass
    @dataclasses.dataclass
    class C6:
        a: typing.Optional[typing.Tuple[str, str]] = None

    C6()
    C6(None)
    C6(("a", "b"))
    with pytest.raises(TypeError):
        C6(1)  # type: ignore
    with pytest.raises(TypeError):
        C6(("a",))  # type: ignore
    with pytest.raises(TypeError):
        C6(("a", "b", "c"))  # type: ignore
    with pytest.raises(TypeError):
        C6(("a", 1))  # type: ignore

    @common.strict_dataclass
    @dataclasses.dataclass
    class C7:
        addr_info: typing.List[common.IPRouteAddressInfoEntry]

        def _post_check(self) -> None:
            pass

    with pytest.raises(TypeError):
        C7(None)  # type: ignore
    C7([])
    C7([common.IPRouteAddressInfoEntry('inet', '169.254.2.115')])
    with pytest.raises(TypeError):
        C7([common.IPRouteAddressInfoEntry('inet', '169.254.2.115'), None])  # type: ignore

    @common.strict_dataclass
    @dataclasses.dataclass
    class C8:
        a: str

        def _post_check(self) -> None:
            if self.a == "invalid":
                raise ValueError("_post_check() failed")

    with pytest.raises(TypeError):
        C8(None)  # type: ignore
    C8("hi")
    with pytest.raises(ValueError):
        C8("invalid")


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
