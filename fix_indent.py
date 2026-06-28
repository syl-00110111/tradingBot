import sys
import re

def fix_file(filename):
    with open(filename, 'r') as f:
        lines = f.readlines()

    new_lines = []
    current_indent = 0
    for line in lines:
        stripped = line.lstrip()
        if not stripped:
            new_lines.append('\n')
            continue

        # This is hard because I don't know the intended nesting.
        # Let's try to just fix the blocks that are known to be wrong.
        new_lines.append(line)

    # Re-writing with specific fixes
    content = "".join(new_lines)

    # Fix load_config_from_path
    content = content.replace("        with open(path, 'r') as f:\n        return json.load(f)", "        with open(path, 'r') as f:\n            return json.load(f)")

    # Fix load_config
    content = content.replace("        if os.path.exists(p):\n        path = p\n        break", "        if os.path.exists(p):\n            path = p\n            break")

    # Fix precision_to_int
    content = content.replace("        if p > 0:\n        return max(0, int(-math.log10(p)))", "        if p > 0:\n            return max(0, int(-math.log10(p)))")

    with open(filename, 'w') as f:
        f.write(content)

fix_file('bot2.py')
