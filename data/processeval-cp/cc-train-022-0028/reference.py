import sys
input = sys.stdin.readline

def indexes(L,R):
    INDLIST=[]

    R-=1
    
    L>>=1
    R>>=1

    while L!=R:
        if L>R:
            INDLIST.append(L)
            L>>=1
        else:
            INDLIST.append(R)
            R>>=1

    while L!=0:
        INDLIST.append(L)
        L>>=1

    return INDLIST

def updates(l,r,x):
        
    L=l+seg_el
    R=r+seg_el

    L//=(L & (-L))
    R//=(R & (-R))

    UPIND=indexes(L,R)
    
    for ind in UPIND[::-1]:
        if LAZY[ind]!=None:
            update_lazy = LAZY[ind] *(1<<(seg_height - 1 - (ind.bit_length())))
            LAZY[ind<<1]=LAZY[1+(ind<<1)]=LAZY[ind]
            SEG[ind<<1]=SEG[1+(ind<<1)]=update_lazy
            LAZY[ind]=None

    while L!=R:
        if L > R:
            SEG[L]=x * (1<<(seg_height - (L.bit_length())))
            LAZY[L]=x
            L+=1
            L//=(L & (-L))

        else:
            R-=1
            SEG[R]=x * (1<<(seg_height - (R.bit_length())))
            LAZY[R]=x
            R//=(R & (-R))

    for ind in UPIND:
        SEG[ind]=SEG[ind<<1]+SEG[1+(ind<<1)]

def getvalues(l,r):

    L=l+seg_el
    R=r+seg_el

    L//=(L & (-L))
    R//=(R & (-R))

    UPIND=indexes(L,R)
    
    for ind in UPIND[::-1]:
        if LAZY[ind]!=None:
            update_lazy = LAZY[ind] *(1<<(seg_height - 1 - (ind.bit_length())))
            LAZY[ind<<1]=LAZY[1+(ind<<1)]=LAZY[ind]
            SEG[ind<<1]=SEG[1+(ind<<1)]=update_lazy
            LAZY[ind]=None
            
    ANS=0

    while L!=R:
        if L > R:
            ANS+=SEG[L]
            L+=1
            L//=(L & (-L))

        else:
            R-=1
            ANS+=SEG[R]
            R//=(R & (-R))

    return ANS

t=int(input())
for tests in range(t):
    n,q=map(int,input().split())
    S=input().strip()
    F=input().strip()
    Q=[tuple(map(int,input().split())) for i in range(q)]

    seg_el=1<<(n.bit_length())
    seg_height=1+n.bit_length()
    SEG=[0]*(2*seg_el)
    LAZY=[None]*(2*seg_el)

    for i in range(n):
        SEG[i+seg_el]=int(F[i])

    for i in range(seg_el-1,0,-1):
        SEG[i]=SEG[i*2]+SEG[i*2+1]

    for l,r in Q[::-1]:
        SUM=r-l+1
        xx=getvalues(l-1,r)

        if xx*2==SUM:
            print("NO")
            break
        else:
            if xx*2>SUM:
                updates(l-1,r,1)
            else:
                updates(l-1,r,0)
    else:
        for i in range(n):
            if getvalues(i,i+1)==int(S[i]):
                True
            else:
                print("NO")
                break
        else:
            print("YES")
                
                
        
        

    


    
