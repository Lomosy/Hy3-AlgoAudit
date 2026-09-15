for s in [*open(0)][2::2]:a=sorted(map(int,s.split()));print(min([j-i for i, j in zip(a,a[1:])]))
