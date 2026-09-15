a,b=map(int,input().split())
z=[]
g=10**9+7
def f():
    return map(int,input().split())
if b==0:
    print (0)
else:
    s=set()
    for i in range(b):
        x,y=f()
        z.append((x,y))
        s.add(x)
        s.add(y)
    s.add (0)
    s.add (a)
    s = sorted(list(s))
    a=len(s)-1
    s=dict([(s[j],j) for j in range(a+1)])
    z=[(s[x],s[y]) for (x,y)in z]
    z.sort(key=lambda x:x[1])
    x=[0]*(a+1)
    x[0]=1
    y=[0]*(a+2)
    i=0
    j=0
    for i in range (a+1):
        while j<b and z[j][1]==i:
            q,p=z[j]
            x[p]+=y[p]-y[q]
            j+=1
        y[i+1]=y[i]+x[i]
        y[i+1]%=g
    print (x[a]%g)