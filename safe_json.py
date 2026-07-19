import json
import os
import shutil

def atomic_write_json(path, data, backup=True, indent=4):
    """Write JSON data to path atomically.

    - Writes to a temporary file then os.replace to avoid partial files on interruption.
    - Optionally creates a backup of the previous file at path + '.bak'.
    """
    tmp = path + '.tmp'
    try:
        # create backup
        if backup and os.path.exists(path):
            try:
                shutil.copy2(path, path + '.bak')
            except Exception:
                # non-fatal
                pass
        # write to temp file
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        # atomic replace
        os.replace(tmp, path)
        return True
    except Exception:
        # cleanup tmp if present
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
