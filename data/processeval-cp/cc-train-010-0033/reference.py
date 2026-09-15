n=int(input())
ar=[float('inf')]*(n+1)
ar[0]=0
for i in range(n):
    t,c=map(int,input().split())
    for j in range(n-1,-1,-1):
        w=min(j+t+1,n)
        ar[w]=min(ar[w],ar[j]+c)
print(ar[n])
