"""Best-effort disk-growth monitoring on Linux. Released snapshots have separate checked quotas."""
import os
import stat


def sample_tree(path, byte_limit, file_limit):
    """Never follow a student-controlled link, including during a concurrent rename."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    total = entries = 0
    complete = True

    def visit(fd, depth):
        nonlocal total, entries, complete
        if depth > 32:
            complete = False
            return
        with os.scandir(fd) as children:
            for child in children:
                if entries > file_limit or total > byte_limit:
                    return
                entries += 1
                try:
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        nested = os.open(child.name, flags, dir_fd=fd)
                        try:
                            visit(nested, depth+1)
                        finally:
                            os.close(nested)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                except OSError:
                    complete = False

    fd = os.open(path, flags)
    try:
        visit(fd, 0)
    finally:
        os.close(fd)
    return {"bytes": total, "entries": entries, "complete": complete,
            "over_limit": total > byte_limit or entries > file_limit}
