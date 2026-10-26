#!/usr/bin/env python3
"""Run a command in a terminal and answer its questions, like a person would.

    answer.py ANSWERS_FILE -- COMMAND [ARGS...]

ANSWERS_FILE has one line per question: TEXT_TO_WAIT_FOR<TAB>ANSWER.
An empty ANSWER presses Enter. Everything the command prints is shown.
Exits with the command's exit code, or 1 if a question did not come.
"""

import os
import pty
import select
import sys

TIMEOUT = 180


def main() -> int:
    split = sys.argv.index("--")
    with open(sys.argv[1], encoding="utf-8") as handle:
        pairs = [(line.rstrip("\n").split("\t", 1) + [""])[:2] for line in handle if line.strip()]
    command = sys.argv[split + 1:]

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)

    seen = ""  # output since the last answer
    done = 0
    while True:
        ready, _, _ = select.select([fd], [], [], TIMEOUT)
        if not ready:
            waiting = pairs[done][0] if done < len(pairs) else "the end"
            print(f"\nanswer.py: nothing new for {TIMEOUT} seconds, waiting for {waiting!r}", file=sys.stderr)
            os.kill(pid, 9)
            break
        try:
            data = os.read(fd, 4096)
        except OSError:  # the command closed the terminal
            break
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        seen += text
        while done < len(pairs) and pairs[done][0] in seen:
            seen = seen[seen.index(pairs[done][0]) + len(pairs[done][0]):]
            os.write(fd, (pairs[done][1] + "\n").encode())
            done += 1

    _, status = os.waitpid(pid, 0)
    if done < len(pairs):
        print(f"answer.py: only {done} of {len(pairs)} questions came.", file=sys.stderr)
        return 1
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
