for _ in range(int(input())):
    s=input().strip()
    one=s.find("11")
    l=s[one:].find("00")
    print("YES" if l<0 else "NO")