import sys

def fix_try_except_with(filename):
    with open(filename, 'r') as f:
        lines = f.readlines()

    new_lines = []
    for i in range(len(lines)):
        line = lines[i]
        new_lines.append(line)
        if i < len(lines) - 1:
            next_line = lines[i+1]
            if line.strip().endswith(':') and not next_line.startswith(line[:len(line)-len(line.lstrip())] + ' '):
                 # This is a very rough heuristic to indent the next line if it's not already
                 # But autopep8 should have done some work.
                 pass

    # Manually fixing known issues based on compile errors
    content = "".join(lines)

    # fix 'try:' at 303
    content = content.replace("    while not shutdown_event.is_set():\n        try:\n        loop = asyncio.get_event_loop()",
                             "    while not shutdown_event.is_set():\n        try:\n            loop = asyncio.get_event_loop()")

    # fix 'if prev_ts != candle_ts:'
    content = content.replace("        if prev_ts != candle_ts:\n            if buy_candidate:\n             consecutive_buys += 1\n             consecutive_sells = 0",
                             "        if prev_ts != candle_ts:\n            if buy_candidate:\n                consecutive_buys += 1\n                consecutive_sells = 0")

    with open(filename, 'w') as f:
        f.write(content)

fix_try_except_with('bot2.py')
