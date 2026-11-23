"""Golden Years 的本地文件整理逻辑。"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

PHOTO = set(".jpg .jpeg .png .heic .heif .webp .bmp .tif .tiff .gif .dng".split())
VIDEO = set(".mp4 .mov .m4v .avi .mkv .wmv .mts .m2ts .3gp".split())
BLOCK = 1024 * 1024


def category(path):
    suffix = path.suffix.lower()
    if suffix in PHOTO:
        return "Photos"
    if suffix in VIDEO:
        return "Videos"


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def directory(path):
    info = path.lstat()
    if linked(info) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"目录不可用或为链接：{path}")


def check_dirs(path):
    for part in (*reversed(path.parents), path):
        directory(part)


@dataclass(frozen=True)
class File:
    path: Path
    size: int
    modified: int
    device: int
    inode: int
    accessed: int

    @classmethod
    def read(cls, path):
        check_dirs(path.parent)
        info = path.lstat()
        if linked(info) or not stat.S_ISREG(info.st_mode):
            raise OSError(f"非常规文件：{path}")
        return cls(path, info.st_size, info.st_mtime_ns, info.st_dev,
                   info.st_ino, info.st_atime_ns)

    def check(self):
        current = File.read(self.path)
        if (self.size, self.modified, self.device, self.inode) != (
            current.size, current.modified, current.device, current.inode
        ):
            raise OSError(f"文件已变化：{self.path}")


def digest(file):
    file.check()
    result = hashlib.sha256()
    with file.path.open("rb") as stream:
        while chunk := stream.read(BLOCK):
            result.update(chunk)
    file.check()
    return result.hexdigest()


def identical(left, right):
    left.check()
    right.check()
    with left.path.open("rb") as a, right.path.open("rb") as b:
        while True:
            chunk = a.read(BLOCK)
            if chunk != b.read(BLOCK):
                return False
            if not chunk:
                break
    left.check()
    right.check()
    return True


def publish(source, target):
    if os.name == "nt":
        os.rename(source, target)  # Windows 拒绝替换已有目标
    else:
        os.link(source, target)
        source.unlink()


def move(file, target):
    file.check()
    check_dirs(target.parent)
    try:
        publish(file.path, target)
        return
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, "winerror", None) != 17:
            raise

    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".partial",
                                     delete=False) as temp:
        partial = Path(temp.name)
        try:
            with file.path.open("rb") as source:
                shutil.copyfileobj(source, temp, BLOCK)
            temp.flush()
            os.fsync(temp.fileno())
        except OSError as error:
            raise OSError(f"复制失败，源文件保留；临时文件 {partial}：{error}") from error
    copied = File.read(partial)
    if copied.size != file.size or digest(copied) != digest(file):
        raise OSError(f"复制校验失败，临时文件：{partial}")
    os.utime(partial, ns=(file.accessed, file.modified))
    file.check()
    check_dirs(target.parent)
    publish(partial, target)
    file.check()
    file.path.unlink()


def archive_files(root):
    for year in sorted(root.iterdir()):
        if not re.fullmatch(r"[0-9]{4}年", year.name):
            continue
        for month in real_children(year):
            if not re.fullmatch(r"(?:[1-9]|1[0-2])月", month.name):
                continue
            for folder in real_children(month):
                if folder.name not in ("Photos", "Videos"):
                    continue
                for path in real_children(folder):
                    info = path.lstat()
                    if category(path) and stat.S_ISREG(info.st_mode) and not linked(info):
                        yield File.read(path)


def real_children(path):
    info = path.lstat()
    if not linked(info) and stat.S_ISDIR(info.st_mode):
        check_dirs(path)
        return sorted(path.iterdir())
    return ()


def make_dirs(root, folder, created):
    current = root
    for name in folder.relative_to(root).parts:
        current = current / name
        check_dirs(current.parent)
        try:
            current.mkdir()
            created.append(current)
        except FileExistsError:
            pass
        directory(current)


def destination(file, folder, prefix, counters):
    occupied = {p.name.casefold() for p in folder.iterdir()}
    if prefix:
        if folder not in counters:
            pattern = re.compile(re.escape(prefix.lower()) + r"_([0-9]+)\.[^.]+")
            counters[folder] = max((int(m[1]) for name in occupied
                                    if (m := pattern.fullmatch(name))), default=0)
        index = counters[folder] + 1
        while True:
            name = f"{prefix}_{index:02}{file.path.suffix}"
            if name.casefold() not in occupied:
                return folder / name, index
            index += 1
    name = file.path.name
    index = 0
    while name.casefold() in occupied:
        index += 1
        name = f"{file.path.stem}_{index}{file.path.suffix}"
    return folder / name, None


def append_log(path, record):
    check_dirs(path.parent)
    if os.path.lexists(path):
        File.read(path)
        if path.stat().st_nlink != 1:
            raise OSError(f"日志包含硬链接：{path}")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def organize(root, report=print, progress=None):
    root = Path(root).absolute()
    check_dirs(root)
    counts = Counter()
    files = []
    for path in list(root.iterdir()):
        kind = category(path)
        try:
            info = path.lstat()
            if linked(info) or not stat.S_ISREG(info.st_mode) or not kind:
                counts["skipped"] += int(not stat.S_ISDIR(info.st_mode))
                continue
            files.append(File.read(path))
        except OSError as error:
            counts["failed", kind] += 1
            report(f"{path.name}：{error}")
    def summary(label, key):
        report(f"{label}：照片 {counts[key, 'Photos']}，视频 {counts[key, 'Videos']}")

    for file in files:
        counts["scan", category(file.path)] += 1
    summary("扫描完成", "scan")
    completed = counts["failed", "Photos"] + counts["failed", "Videos"]
    total = completed + len(files)
    if progress is not None:
        progress(completed, total)
    index = defaultdict(list)
    hashes = {}
    counters = {}
    if files:
        for existing in archive_files(root):
            index[existing.size].append(existing)

    def fingerprint(file):
        file.check()
        if file not in hashes:
            hashes[file] = digest(file)
        return hashes[file]

    files.sort(key=lambda f: (f.modified, f.path.name.casefold(), f.path.name))
    started = datetime.now()
    log = root / "XXX" / started.strftime("%Y%m_重复文件日志.txt")
    for file in files:
        kind = category(file.path)
        created = []
        try:
            file.check()
            original = next((other for other in index[file.size]
                             if fingerprint(file) == fingerprint(other)
                             and identical(file, other)), None)
            date = datetime.fromtimestamp(file.modified // 1_000_000_000)
            folder = root / f"{date.year:04}年" / f"{date.month}月" / kind
            prefix = ("MQP" if kind == "Photos" else "MQV") + date.strftime("%Y%m")
            if original:
                folder, prefix = log.parent, None
            make_dirs(root, folder, created)
            target, number = destination(file, folder, prefix, counters)
            if original:
                original.check()
            move(file, target)
            if original:
                original.check()
                counts["duplicate", kind] += 1
                try:
                    append_log(log, dict(时间=started.isoformat(), 原文件=file.path.name,
                                         重复件=str(target.relative_to(root)),
                                         正本=str(original.path.relative_to(root)),
                                         字节=file.size, SHA256=hashes[file]))
                    counts["logged"] += 1
                except OSError as error:
                    counts["log_error"] += 1
                    report(f"{target.name} 已移入 XXX，写入日志失败：{error}")
            else:
                counters[folder] = number
                index[file.size].append(File.read(target))
                counts["archived", kind] += 1
        except (OSError, ValueError, OverflowError) as error:
            counts["failed", kind] += 1
            report(f"{file.path.name}：{error}")
            for folder in reversed(created):
                try:
                    folder.rmdir()  # 仅回收本次创建且仍为空的目录
                except OSError:
                    break
        completed += 1
        if progress is not None:
            progress(completed, total)
    summary("成功归档", "archived")
    summary("重复文件", "duplicate")
    summary("失败", "failed")
    report(f"跳过文件：{counts['skipped']}")
    if counts["logged"]:
        report(f"重复日志：{log.relative_to(root)}（写入 {counts['logged']} 条）")
    elif not counts["log_error"]:
        report("重复文件：0；本次未生成重复日志")
    errors = counts["log_error"] or any(v for k, v in counts.items()
                                        if isinstance(k, tuple) and k[0] == "failed")
    report("操作结束，存在错误，请查看上方记录" if errors else "全部操作完成")
    return counts
