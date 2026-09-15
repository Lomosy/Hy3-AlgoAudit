import sys


def main():
    data = sys.stdin.buffer.read().split()
    n = int(data[0])
    best = None
    cur = 0
    for i in range(n):
        x = int(data[1 + i])
        cur = x if (cur <= 0) else cur + x
        if best is None or cur > best:
            best = cur
    print(best)


main()
