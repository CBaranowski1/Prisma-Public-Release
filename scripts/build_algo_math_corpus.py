"""Inject a programming-math + algorithms REFERENCE corpus into the brain.

GENERAL technique notes (the competitive toolkit) - NOT solutions to any test
problem - so brain-assisted runs stay honest. Densely wikilinked to siblings +
[[algorithms-index]]. Written as .md into the vault; lexical recall reads them
live (no reindex), so a running exam picks them up on the next problem.

  python scripts/build_algo_math_corpus.py --vault C:\\dev\\vault
"""
from __future__ import annotations
import argparse
import json
import os

def _default_vault():
    base=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bj=os.path.join(base,"brains","llm-council","brain.json")
    try:
        vp=(json.load(open(bj,encoding="utf-8")).get("vault_path") or "").strip()
        if vp: return os.path.normpath(os.path.join(os.path.dirname(bj),vp))
    except Exception: pass
    return os.path.join(base,"brains","llm-council","vault")

NOTES = [('number-theory',
  'sieve-of-eratosthenes',
  'Sieve of Eratosthenes',
  'All primes up to n in O(n log log n).',
  ['prime-factorization', 'miller-rabin'],
  'def sieve(n):\n'
  '    p=[True]*(n+1); p[0]=p[1]=False\n'
  '    for i in range(2,int(n**0.5)+1):\n'
  '        if p[i]:\n'
  '            for j in range(i*i,n+1,i): p[j]=False\n'
  '    return [i for i,v in enumerate(p) if v]'),
 ('number-theory',
  'prime-factorization',
  'Prime Factorization',
  'Factor n in O(sqrt n); returns prime->exponent.',
  ['sieve-of-eratosthenes', 'divisors'],
  'def factor(n):\n    f={}; d=2\n    while d*d<=n:\n        while n%d==0: f[d]=f.get(d,0)+1; n//=d\n        d+=1\n    if n>1: f[n]=f.get(n,0)+1\n    return f'),
 ('number-theory', 'gcd-lcm', 'GCD and LCM', 'Euclidean gcd; lcm divides first to avoid overflow.', ['extended-euclid', 'modular-inverse'], 'from math import gcd\ndef lcm(a,b): return a//gcd(a,b)*b'),
 ('number-theory',
  'extended-euclid',
  'Extended Euclid + Inverse',
  'Solves ax+by=gcd; gives modular inverse for any coprime modulus.',
  ['gcd-lcm', 'modular-inverse', 'chinese-remainder-theorem'],
  'def egcd(a,b):\n    if b==0: return a,1,0\n    g,x,y=egcd(b,a%b)\n    return g,y,x-(a//b)*y\ndef modinv(a,m):\n    g,x,_=egcd(a%m,m)\n    return x%m if g==1 else None'),
 ('number-theory',
  'modular-arithmetic',
  'Modular Arithmetic (1e9+7)',
  'Add/sub/mul under MOD; division needs the inverse.',
  ['modular-exponentiation', 'modular-inverse', 'combinations-mod'],
  'MOD=10**9+7\n# (a+b)%MOD, (a*b)%MOD ; a/b -> a*pow(b,MOD-2,MOD)%MOD'),
 ('number-theory',
  'modular-exponentiation',
  'Fast Power',
  'a^b mod m in O(log b); pow(a,b,m) is built in.',
  ['modular-arithmetic', 'matrix-exponentiation'],
  'def power(a,b,m):\n    r=1; a%=m\n    while b:\n        if b&1: r=r*a%m\n        a=a*a%m; b>>=1\n    return r'),
 ('number-theory',
  'modular-inverse',
  'Modular Inverse',
  'Fermat for prime modulus: a^(m-2); else use extended Euclid.',
  ['modular-exponentiation', 'extended-euclid', 'combinations-mod'],
  'def inv(a,m=10**9+7): return pow(a,m-2,m)'),
 ('number-theory',
  'chinese-remainder-theorem',
  'Chinese Remainder Theorem',
  'Merge two congruences x=r1(m1), x=r2(m2).',
  ['extended-euclid'],
  'def crt(r1,m1,r2,m2):\n'
  '    from math import gcd\n'
  '    g=gcd(m1,m2)\n'
  '    if (r2-r1)%g: return None\n'
  '    l=m1//g*m2\n'
  '    def egcd(a,b):\n'
  '        if b==0: return a,1,0\n'
  '        gg,x,y=egcd(b,a%b); return gg,y,x-(a//b)*y\n'
  '    _,p,_=egcd(m1//g,m2//g)\n'
  '    x=(r1+(r2-r1)//g*p%(m2//g)*m1)%l\n'
  '    return x,l'),
 ('number-theory',
  'miller-rabin',
  'Miller-Rabin Primality',
  'Deterministic for n < 2^64 with fixed bases.',
  ['sieve-of-eratosthenes'],
  'def is_prime(n):\n'
  '    if n<2: return False\n'
  '    for p in (2,3,5,7,11,13,17,19,23,29,31,37):\n'
  '        if n%p==0: return n==p\n'
  '    d=n-1; s=0\n'
  '    while d%2==0: d//=2; s+=1\n'
  '    for a in (2,3,5,7,11,13,17,19,23,29,31,37):\n'
  '        x=pow(a,d,n)\n'
  '        if x==1 or x==n-1: continue\n'
  '        for _ in range(s-1):\n'
  '            x=x*x%n\n'
  '            if x==n-1: break\n'
  '        else: return False\n'
  '    return True'),
 ('number-theory',
  'divisors',
  'All Divisors / Count / Sum',
  'Enumerate divisors in O(sqrt n).',
  ['prime-factorization'],
  'def divisors(n):\n    ds=[]; i=1\n    while i*i<=n:\n        if n%i==0:\n            ds.append(i)\n            if i!=n//i: ds.append(n//i)\n        i+=1\n    return sorted(ds)'),
 ('number-theory',
  'fast-doubling-fibonacci',
  'Fibonacci (fast doubling)',
  'nth Fibonacci in O(log n).',
  ['matrix-exponentiation'],
  'def fib(n):\n    def fd(n):\n        if n==0: return (0,1)\n        a,b=fd(n>>1)\n        c=a*(2*b-a); d=a*a+b*b\n        return (d,c+d) if n&1 else (c,d)\n    return fd(n)[0]'),
 ('number-theory',
  'matrix-exponentiation',
  'Matrix Exponentiation',
  'Linear recurrences in O(k^3 log n); powers a transition matrix.',
  ['modular-exponentiation', 'fast-doubling-fibonacci'],
  'def mul(A,B,m):\n'
  '    n=len(A); k=len(B); p=len(B[0])\n'
  '    C=[[0]*p for _ in range(n)]\n'
  '    for i in range(n):\n'
  '        for t in range(k):\n'
  '            if A[i][t]:\n'
  '                for j in range(p): C[i][j]=(C[i][j]+A[i][t]*B[t][j])%m\n'
  '    return C\n'
  'def mpow(M,e,m):\n'
  '    n=len(M); R=[[int(i==j) for j in range(n)] for i in range(n)]\n'
  '    while e:\n'
  '        if e&1: R=mul(R,M,m)\n'
  '        M=mul(M,M,m); e>>=1\n'
  '    return R'),
 ('combinatorics',
  'factorials',
  'Precomputed Factorials',
  'O(n) factorials + inverse factorials for fast nCr.',
  ['combinations-mod', 'modular-inverse'],
  'MOD=10**9+7; N=200001\nf=[1]*N; inv=[1]*N\nfor i in range(1,N): f[i]=f[i-1]*i%MOD\ninv[N-1]=pow(f[N-1],MOD-2,MOD)\nfor i in range(N-1,0,-1): inv[i-1]=inv[i]*i%MOD'),
 ('combinatorics',
  'combinations-mod',
  'nCr mod p',
  'Binomial coefficients under a prime modulus, O(1) per query.',
  ['factorials', 'modular-inverse', 'pascals-triangle'],
  'MOD=10**9+7\ndef nCr(n,r,f,inv):\n    if r<0 or r>n: return 0\n    return f[n]*inv[r]%MOD*inv[n-r]%MOD'),
 ('combinatorics',
  'pascals-triangle',
  "Pascal's Triangle",
  'C(n,k)=C(n-1,k-1)+C(n-1,k); good for small n / no modulus.',
  ['combinations-mod', 'catalan-numbers'],
  'def pascal(n):\n    C=[[1]*(i+1) for i in range(n+1)]\n    for i in range(2,n+1):\n        for k in range(1,i): C[i][k]=C[i-1][k-1]+C[i-1][k]\n    return C'),
 ('combinatorics',
  'catalan-numbers',
  'Catalan Numbers',
  'Count balanced parens, BSTs, triangulations. C_n=C(2n,n)/(n+1).',
  ['combinations-mod', 'pascals-triangle'],
  'def catalan(n):\n    c=[0]*(n+1); c[0]=1\n    for i in range(1,n+1):\n        for j in range(i): c[i]+=c[j]*c[i-1-j]\n    return c[n]'),
 ('combinatorics',
  'stars-and-bars',
  'Stars and Bars',
  'Ways to put n identical items into k bins = C(n+k-1, k-1).',
  ['combinations-mod'],
  '# nonneg solutions of x1+...+xk = n  is  C(n+k-1, k-1)\n# positive solutions is C(n-1, k-1)'),
 ('combinatorics',
  'inclusion-exclusion',
  'Inclusion-Exclusion',
  'Union counts by alternating sums over subsets.',
  ['combinations-mod', 'bit-manipulation'],
  '# |A u B u C| = |A|+|B|+|C| - |AB|-|AC|-|BC| + |ABC|\n# iterate subsets of constraints, sign = (-1)^(popcount-1)'),
 ('combinatorics',
  'derangements',
  'Derangements',
  'Permutations with no fixed point: D(n)=(n-1)(D(n-1)+D(n-2)).',
  ['factorials'],
  'def derange(n,m=10**9+7):\n    a,b=1,0  # D(0),D(1)\n    for i in range(2,n+1): a,b=b,(i-1)*(b+a)%m\n    return a if n==0 else b'),
 ('arrays',
  'prefix-sums',
  'Prefix Sums',
  'Range sum in O(1) after O(n) precompute.',
  ['difference-array', 'kadane-maximum-subarray'],
  'pre=[0]*(len(a)+1)\nfor i,x in enumerate(a): pre[i+1]=pre[i]+x\n# sum a[l..r] = pre[r+1]-pre[l]'),
 ('arrays',
  'difference-array',
  'Difference Array',
  'O(1) range updates, materialize with one prefix sum.',
  ['prefix-sums'],
  'd=[0]*(n+1)\n# add v to a[l..r]: d[l]+=v; d[r+1]-=v\nfor i in range(1,n): d[i]+=d[i-1]'),
 ('arrays',
  'two-pointers',
  'Two Pointers',
  'O(n) pair/subarray search on sorted data.',
  ['sliding-window', 'binary-search'],
  'def two_sum_sorted(a,t):\n    i,j=0,len(a)-1\n    while i<j:\n        s=a[i]+a[j]\n        if s==t: return (i,j)\n        i+=s<t; j-=s>t\n    return None'),
 ('arrays',
  'sliding-window',
  'Sliding Window',
  'Moving window with running aggregate; O(n).',
  ['two-pointers', 'prefix-sums'],
  'from collections import Counter\n'
  'def longest_k_distinct(s,k):\n'
  '    c=Counter(); l=best=0\n'
  '    for r,ch in enumerate(s):\n'
  '        c[ch]+=1\n'
  '        while len(c)>k:\n'
  '            c[s[l]]-=1\n'
  '            if c[s[l]]==0: del c[s[l]]\n'
  '            l+=1\n'
  '        best=max(best,r-l+1)\n'
  '    return best'),
 ('arrays',
  'kadane-maximum-subarray',
  'Kadane Maximum Subarray',
  'Max contiguous sum in O(n).',
  ['prefix-sums'],
  'def kadane(a):\n    best=cur=a[0]\n    for x in a[1:]: cur=max(x,cur+x); best=max(best,cur)\n    return best'),
 ('arrays',
  'binary-search',
  'Binary Search / bisect',
  "O(log n) search; 'search on the answer' for feasibility.",
  ['two-pointers', 'ternary-search'],
  'import bisect\ni=bisect.bisect_left(a,x)\n# answer search: lo,hi\n# while lo<hi: mid=(lo+hi)//2; (hi:=mid) if ok(mid) else (lo:=mid+1)'),
 ('arrays',
  'coordinate-compression',
  'Coordinate Compression',
  'Map big/sparse values to 0..k-1 indices.',
  ['binary-search', 'fenwick-bit'],
  'xs=sorted(set(vals)); idx={x:i for i,x in enumerate(xs)}\nc=[idx[v] for v in vals]'),
 ('arrays',
  'merge-sort-inversions',
  'Merge Sort + Inversions',
  'Count inversions while sorting in O(n log n).',
  ['sorting-tricks'],
  'def sort_count(a):\n'
  '    if len(a)<=1: return a,0\n'
  '    m=len(a)//2; L,x=sort_count(a[:m]); R,y=sort_count(a[m:])\n'
  '    out=[]; i=j=inv=0\n'
  '    while i<len(L) and j<len(R):\n'
  '        if L[i]<=R[j]: out.append(L[i]); i+=1\n'
  '        else: out.append(R[j]); j+=1; inv+=len(L)-i\n'
  '    out+=L[i:]+R[j:]\n'
  '    return out,x+y+inv'),
 ('arrays', 'sorting-tricks', 'Sorting with Keys', 'Multi-key sort via tuples; reverse with negation.', ['binary-search'], 'a.sort(key=lambda p:(p[0],-p[1]))'),
 ('strings',
  'kmp',
  'KMP Pattern Matching',
  'All occurrences in O(n+m) via failure function.',
  ['z-algorithm', 'string-hashing'],
  'def kmp(s,p):\n'
  '    f=[0]*len(p); k=0\n'
  '    for i in range(1,len(p)):\n'
  '        while k and p[i]!=p[k]: k=f[k-1]\n'
  '        if p[i]==p[k]: k+=1\n'
  '        f[i]=k\n'
  '    res=[]; k=0\n'
  '    for i,c in enumerate(s):\n'
  '        while k and c!=p[k]: k=f[k-1]\n'
  '        if c==p[k]: k+=1\n'
  '        if k==len(p): res.append(i-k+1); k=f[k-1]\n'
  '    return res'),
 ('strings',
  'z-algorithm',
  'Z-Algorithm',
  'Z[i]=longest prefix match starting at i; O(n).',
  ['kmp', 'manacher'],
  'def z_func(s):\n'
  '    n=len(s); z=[0]*n; l=r=0\n'
  '    for i in range(1,n):\n'
  '        if i<r: z[i]=min(r-i,z[i-l])\n'
  '        while i+z[i]<n and s[z[i]]==s[i+z[i]]: z[i]+=1\n'
  '        if i+z[i]>r: l,r=i,i+z[i]\n'
  '    return z'),
 ('strings',
  'string-hashing',
  'Polynomial String Hashing',
  'O(1) substring comparison after O(n) build.',
  ['kmp'],
  'MOD=(1<<61)-1; B=131\n'
  'def build(s):\n'
  '    h=[0]*(len(s)+1); p=[1]*(len(s)+1)\n'
  '    for i,c in enumerate(s):\n'
  '        h[i+1]=(h[i]*B+ord(c))%MOD; p[i+1]=p[i]*B%MOD\n'
  '    return h,p\n'
  '# hash s[l:r] = (h[r]-h[l]*p[r-l])%MOD'),
 ('strings',
  'manacher',
  'Manacher (longest palindrome)',
  'Longest palindromic substring length in O(n).',
  ['z-algorithm', 'dp-longest-palindromic-subsequence'],
  'def manacher(s):\n'
  "    t='#'+'#'.join(s)+'#'; n=len(t); p=[0]*n; c=r=0\n"
  '    for i in range(n):\n'
  '        if i<r: p[i]=min(r-i,p[2*c-i])\n'
  '        while i-p[i]-1>=0 and i+p[i]+1<n and t[i-p[i]-1]==t[i+p[i]+1]: p[i]+=1\n'
  '        if i+p[i]>r: c,r=i,i+p[i]\n'
  '    return max(p)'),
 ('strings',
  'trie',
  'Trie (prefix tree)',
  'Insert/search words; powers autocomplete, XOR-max.',
  ['bit-manipulation'],
  'class Trie:\n'
  '    def __init__(s): s.ch={}; s.end=False\n'
  '    def add(s,w):\n'
  '        n=s\n'
  '        for c in w: n=n.ch.setdefault(c,Trie())\n'
  '        n.end=True\n'
  '    def has(s,w):\n'
  '        n=s\n'
  '        for c in w:\n'
  '            if c not in n.ch: return False\n'
  '            n=n.ch[c]\n'
  '        return n.end'),
 ('dp',
  'dp-fibonacci-memo',
  'DP: Memoization',
  'Cache overlapping subproblems with lru_cache.',
  ['dp-coin-change', 'dp-knapsack-01'],
  'from functools import lru_cache\n@lru_cache(None)\ndef fib(n): return n if n<2 else fib(n-1)+fib(n-2)'),
 ('dp',
  'dp-coin-change',
  'DP: Coin Change',
  'Min coins / ways for an amount (unbounded).',
  ['dp-knapsack-01', 'dp-unbounded-knapsack'],
  "def min_coins(coins,amt):\n    INF=float('inf'); dp=[0]+[INF]*amt\n    for c in coins:\n        for x in range(c,amt+1): dp[x]=min(dp[x],dp[x-c]+1)\n    return dp[amt] if dp[amt]<INF else -1"),
 ('dp',
  'dp-knapsack-01',
  'DP: 0/1 Knapsack',
  'Max value under weight budget, each item once.',
  ['dp-coin-change', 'dp-subset-sum'],
  'def knap(wt,val,W):\n    dp=[0]*(W+1)\n    for w,v in zip(wt,val):\n        for c in range(W,w-1,-1): dp[c]=max(dp[c],dp[c-w]+v)\n    return dp[W]'),
 ('dp',
  'dp-unbounded-knapsack',
  'DP: Unbounded Knapsack',
  'Items reusable; iterate weight ascending.',
  ['dp-knapsack-01', 'dp-coin-change'],
  'def unb(wt,val,W):\n    dp=[0]*(W+1)\n    for c in range(1,W+1):\n        for w,v in zip(wt,val):\n            if w<=c: dp[c]=max(dp[c],dp[c-w]+v)\n    return dp[W]'),
 ('dp',
  'dp-subset-sum',
  'DP: Subset Sum',
  'Can a subset hit target? Boolean knapsack.',
  ['dp-knapsack-01'],
  'def subset_sum(a,t):\n    dp=1  # bitmask of reachable sums\n    for x in a: dp|=dp<<x\n    return (dp>>t)&1==1'),
 ('dp',
  'dp-longest-increasing-subsequence',
  'DP: LIS',
  'Length of longest increasing subsequence, O(n log n).',
  ['binary-search', 'dp-longest-common-subsequence'],
  'import bisect\ndef lis(a):\n    tails=[]\n    for x in a:\n        i=bisect.bisect_left(tails,x)\n        if i==len(tails): tails.append(x)\n        else: tails[i]=x\n    return len(tails)'),
 ('dp',
  'dp-longest-common-subsequence',
  'DP: LCS',
  '2D table, O(nm).',
  ['dp-edit-distance', 'dp-longest-palindromic-subsequence'],
  'def lcs(a,b):\n'
  '    n,m=len(a),len(b); dp=[[0]*(m+1) for _ in range(n+1)]\n'
  '    for i in range(1,n+1):\n'
  '        for j in range(1,m+1):\n'
  '            dp[i][j]=dp[i-1][j-1]+1 if a[i-1]==b[j-1] else max(dp[i-1][j],dp[i][j-1])\n'
  '    return dp[n][m]'),
 ('dp',
  'dp-edit-distance',
  'DP: Edit Distance',
  'Levenshtein min ops, O(nm).',
  ['dp-longest-common-subsequence'],
  'def edit(a,b):\n'
  '    n,m=len(a),len(b); dp=[[0]*(m+1) for _ in range(n+1)]\n'
  '    for i in range(n+1): dp[i][0]=i\n'
  '    for j in range(m+1): dp[0][j]=j\n'
  '    for i in range(1,n+1):\n'
  '        for j in range(1,m+1):\n'
  '            dp[i][j]=dp[i-1][j-1] if a[i-1]==b[j-1] else 1+min(dp[i-1][j],dp[i][j-1],dp[i-1][j-1])\n'
  '    return dp[n][m]'),
 ('dp',
  'dp-longest-palindromic-subsequence',
  'DP: Longest Palindromic Subsequence',
  'LCS of s and reversed s.',
  ['dp-longest-common-subsequence', 'manacher'],
  'def lps(s): return None  # = lcs(s, s[::-1])'),
 ('dp',
  'dp-bitmask',
  'DP: Bitmask (TSP)',
  'State = visited set; O(2^n * n^2).',
  ['bit-manipulation', 'dp-knapsack-01'],
  'def tsp(dist,n):\n'
  "    INF=float('inf'); dp=[[INF]*n for _ in range(1<<n)]; dp[1][0]=0\n"
  '    for mask in range(1<<n):\n'
  '        for u in range(n):\n'
  '            if dp[mask][u]==INF: continue\n'
  '            for v in range(n):\n'
  '                if not mask>>v&1:\n'
  '                    nm=mask|1<<v\n'
  '                    dp[nm][v]=min(dp[nm][v],dp[mask][u]+dist[u][v])\n'
  '    return min(dp[(1<<n)-1])'),
 ('dp',
  'dp-interval',
  'DP: Interval DP',
  'dp[i][j] over a range; combine subranges (matrix chain, burst balloons).',
  ['dp-longest-common-subsequence'],
  '# for length in 2..n: for i: j=i+length-1\n#   dp[i][j]=min over k of dp[i][k]+dp[k+1][j]+cost(i,k,j)'),
 ('dp',
  'dp-tree',
  'DP: Tree DP',
  'Aggregate over children via post-order DFS.',
  ['dfs', 'dp-fibonacci-memo'],
  'import sys; sys.setrecursionlimit(300000)\ndef solve(u,par,adj,dp):\n    dp[u]=1\n    for v in adj[u]:\n        if v!=par: solve(v,u,adj,dp); dp[u]+=dp[v]'),
 ('dp',
  'dp-digit',
  'DP: Digit DP',
  'Count numbers <=N with a digit property; state (pos, tight, ...).',
  ['dp-fibonacci-memo'],
  '# digits=list of N; @lru_cache over (pos, tight, extra)\n# at each pos loop d in 0..(digits[pos] if tight else 9)'),
 ('graphs',
  'bfs',
  'BFS (unweighted shortest path)',
  'Level order with deque.',
  ['dfs', 'dijkstra', 'zero-one-bfs'],
  'from collections import deque\n'
  'def bfs(adj,s):\n'
  '    dist={s:0}; q=deque([s])\n'
  '    while q:\n'
  '        u=q.popleft()\n'
  '        for v in adj[u]:\n'
  '            if v not in dist: dist[v]=dist[u]+1; q.append(v)\n'
  '    return dist'),
 ('graphs',
  'dfs',
  'DFS (iterative + recursion limit)',
  'Reachability, components, cycle detection.',
  ['bfs', 'topological-sort', 'scc-kosaraju'],
  'import sys; sys.setrecursionlimit(300000)\ndef dfs(u,adj,seen):\n    seen.add(u)\n    for v in adj[u]:\n        if v not in seen: dfs(v,adj,seen)'),
 ('graphs',
  'zero-one-bfs',
  '0-1 BFS',
  'Shortest path with 0/1 weights in O(V+E) via deque.',
  ['bfs', 'dijkstra'],
  'from collections import deque\n'
  'def bfs01(adj,s,n):\n'
  "    INF=float('inf'); d=[INF]*n; d[s]=0; dq=deque([s])\n"
  '    while dq:\n'
  '        u=dq.popleft()\n'
  '        for v,w in adj[u]:\n'
  '            if d[u]+w<d[v]:\n'
  '                d[v]=d[u]+w\n'
  '                dq.appendleft(v) if w==0 else dq.append(v)\n'
  '    return d'),
 ('graphs',
  'dijkstra',
  'Dijkstra',
  'Non-negative weighted shortest path, O(E log V).',
  ['bfs', 'heap-priority-queue', 'bellman-ford'],
  'import heapq\n'
  'def dijkstra(adj,s,n):\n'
  "    INF=float('inf'); d=[INF]*n; d[s]=0; pq=[(0,s)]\n"
  '    while pq:\n'
  '        du,u=heapq.heappop(pq)\n'
  '        if du>d[u]: continue\n'
  '        for v,w in adj[u]:\n'
  '            if du+w<d[v]: d[v]=du+w; heapq.heappush(pq,(d[v],v))\n'
  '    return d'),
 ('graphs',
  'bellman-ford',
  'Bellman-Ford',
  'Handles negative edges; detects negative cycles. O(VE).',
  ['dijkstra', 'floyd-warshall'],
  "def bf(edges,n,s):\n    INF=float('inf'); d=[INF]*n; d[s]=0\n    for _ in range(n-1):\n        for u,v,w in edges:\n            if d[u]+w<d[v]: d[v]=d[u]+w\n    return d"),
 ('graphs',
  'floyd-warshall',
  'Floyd-Warshall',
  'All-pairs shortest paths, O(n^3).',
  ['bellman-ford', 'dijkstra'],
  'def floyd(d,n):\n'
  '    for k in range(n):\n'
  '        for i in range(n):\n'
  '            dik=d[i][k]\n'
  '            for j in range(n):\n'
  '                if dik+d[k][j]<d[i][j]: d[i][j]=dik+d[k][j]\n'
  '    return d'),
 ('graphs',
  'union-find-dsu',
  'Union-Find (DSU)',
  'Near-O(1) components; core of Kruskal.',
  ['connected-components', 'mst-kruskal'],
  'class DSU:\n'
  '    def __init__(s,n): s.p=list(range(n)); s.r=[0]*n\n'
  '    def find(s,x):\n'
  '        while s.p[x]!=x: s.p[x]=s.p[s.p[x]]; x=s.p[x]\n'
  '        return x\n'
  '    def union(s,a,b):\n'
  '        a,b=s.find(a),s.find(b)\n'
  '        if a==b: return False\n'
  '        if s.r[a]<s.r[b]: a,b=b,a\n'
  '        s.p[b]=a; s.r[a]+=s.r[a]==s.r[b]; return True'),
 ('graphs',
  'mst-kruskal',
  'MST (Kruskal)',
  'Min spanning tree by sorting edges + DSU.',
  ['union-find-dsu', 'mst-prim'],
  'def kruskal(n,edges):  # edges (w,u,v)\n    dsu=DSU(n); edges.sort(); cost=0\n    for w,u,v in edges:\n        if dsu.union(u,v): cost+=w\n    return cost'),
 ('graphs',
  'mst-prim',
  'MST (Prim)',
  'Grow tree from a node with a heap.',
  ['mst-kruskal', 'dijkstra'],
  'import heapq\n'
  'def prim(adj,n):\n'
  '    seen=[False]*n; pq=[(0,0)]; cost=0\n'
  '    while pq:\n'
  '        w,u=heapq.heappop(pq)\n'
  '        if seen[u]: continue\n'
  '        seen[u]=True; cost+=w\n'
  '        for v,wt in adj[u]:\n'
  '            if not seen[v]: heapq.heappush(pq,(wt,v))\n'
  '    return cost'),
 ('graphs',
  'topological-sort',
  'Topological Sort (Kahn)',
  'DAG ordering; detects cycles.',
  ['bfs', 'dfs'],
  'from collections import deque\n'
  'def topo(adj,n):\n'
  '    indeg=[0]*n\n'
  '    for u in range(n):\n'
  '        for v in adj[u]: indeg[v]+=1\n'
  '    q=deque(i for i in range(n) if indeg[i]==0); order=[]\n'
  '    while q:\n'
  '        u=q.popleft(); order.append(u)\n'
  '        for v in adj[u]:\n'
  '            indeg[v]-=1\n'
  '            if indeg[v]==0: q.append(v)\n'
  '    return order if len(order)==n else None'),
 ('graphs',
  'connected-components',
  'Connected Components',
  'Count/label via DFS/BFS/DSU.',
  ['bfs', 'dfs', 'union-find-dsu'],
  'def components(adj,n):\n'
  '    seen=[False]*n; c=0\n'
  '    for s in range(n):\n'
  '        if not seen[s]:\n'
  '            c+=1; st=[s]; seen[s]=True\n'
  '            while st:\n'
  '                u=st.pop()\n'
  '                for v in adj[u]:\n'
  '                    if not seen[v]: seen[v]=True; st.append(v)\n'
  '    return c'),
 ('graphs',
  'scc-kosaraju',
  'Strongly Connected Components',
  'Kosaraju two-pass DFS, O(V+E).',
  ['dfs', 'topological-sort'],
  'import sys; sys.setrecursionlimit(300000)\n'
  'def scc(adj,n):\n'
  '    radj=[[] for _ in range(n)]\n'
  '    for u in range(n):\n'
  '        for v in adj[u]: radj[v].append(u)\n'
  '    seen=[False]*n; order=[]\n'
  '    def d1(u):\n'
  '        seen[u]=True\n'
  '        for v in adj[u]:\n'
  '            if not seen[v]: d1(v)\n'
  '        order.append(u)\n'
  '    for u in range(n):\n'
  '        if not seen[u]: d1(u)\n'
  '    comp=[-1]*n\n'
  '    def d2(u,c):\n'
  '        comp[u]=c\n'
  '        for v in radj[u]:\n'
  '            if comp[v]==-1: d2(v,c)\n'
  '    c=0\n'
  '    for u in reversed(order):\n'
  '        if comp[u]==-1: d2(u,c); c+=1\n'
  '    return comp,c'),
 ('graphs',
  'bipartite-check',
  'Bipartite Check',
  '2-color with BFS; odd cycle => not bipartite.',
  ['bfs'],
  'from collections import deque\n'
  'def bipartite(adj,n):\n'
  '    color=[-1]*n\n'
  '    for s in range(n):\n'
  '        if color[s]==-1:\n'
  '            color[s]=0; q=deque([s])\n'
  '            while q:\n'
  '                u=q.popleft()\n'
  '                for v in adj[u]:\n'
  '                    if color[v]==-1: color[v]=color[u]^1; q.append(v)\n'
  '                    elif color[v]==color[u]: return False\n'
  '    return True'),
 ('graphs',
  'lca-binary-lifting',
  'LCA (binary lifting)',
  'Lowest common ancestor in O(log n) per query.',
  ['dfs', 'sparse-table-rmq'],
  '# up[k][v] = 2^k-th ancestor; depth[]\n# lift deeper node to equal depth, then lift both until parents match'),
 ('graphs',
  'max-flow-edmonds-karp',
  'Max Flow (Edmonds-Karp)',
  'BFS augmenting paths, O(VE^2).',
  ['bfs'],
  'from collections import deque\n'
  'def max_flow(cap,s,t,n):\n'
  '    flow=0\n'
  '    while True:\n'
  '        par=[-1]*n; par[s]=s; q=deque([s])\n'
  '        while q:\n'
  '            u=q.popleft()\n'
  '            for v in range(n):\n'
  '                if par[v]==-1 and cap[u][v]>0: par[v]=u; q.append(v)\n'
  '        if par[t]==-1: break\n'
  "        aug=float('inf'); v=t\n"
  '        while v!=s: aug=min(aug,cap[par[v]][v]); v=par[v]\n'
  '        v=t\n'
  '        while v!=s: cap[par[v]][v]-=aug; cap[v][par[v]]+=aug; v=par[v]\n'
  '        flow+=aug\n'
  '    return flow'),
 ('data-structures',
  'heap-priority-queue',
  'Heap / Priority Queue',
  'heapq min-heap; negate for max. O(log n).',
  ['dijkstra', 'monotonic-stack'],
  'import heapq\nh=[]; heapq.heappush(h,(prio,item)); prio,item=heapq.heappop(h)\n# heapq.nlargest(k, it)'),
 ('data-structures',
  'fenwick-bit',
  'Fenwick Tree (BIT)',
  'Prefix sums + point updates in O(log n).',
  ['prefix-sums', 'segment-tree', 'coordinate-compression'],
  'class BIT:\n'
  '    def __init__(s,n): s.t=[0]*(n+1)\n'
  '    def add(s,i,v):\n'
  '        i+=1\n'
  '        while i<len(s.t): s.t[i]+=v; i+=i&-i\n'
  '    def sum(s,i):\n'
  '        i+=1; r=0\n'
  '        while i>0: r+=s.t[i]; i-=i&-i\n'
  '        return r'),
 ('data-structures',
  'segment-tree',
  'Segment Tree (iterative)',
  'Point update, range query in O(log n).',
  ['fenwick-bit', 'segment-tree-lazy', 'sparse-table-rmq'],
  'class Seg:\n'
  '    def __init__(s,a):\n'
  '        s.n=len(a); s.t=[0]*(2*s.n); s.t[s.n:]=a\n'
  '        for i in range(s.n-1,0,-1): s.t[i]=s.t[2*i]+s.t[2*i+1]\n'
  '    def upd(s,i,v):\n'
  '        i+=s.n; s.t[i]=v; i//=2\n'
  '        while i: s.t[i]=s.t[2*i]+s.t[2*i+1]; i//=2\n'
  '    def query(s,l,r):  # [l,r)\n'
  '        l+=s.n; r+=s.n; res=0\n'
  '        while l<r:\n'
  '            if l&1: res+=s.t[l]; l+=1\n'
  '            if r&1: r-=1; res+=s.t[r]\n'
  '            l//=2; r//=2\n'
  '        return res'),
 ('data-structures',
  'segment-tree-lazy',
  'Segment Tree (lazy, range add)',
  'Range update + range sum in O(log n).',
  ['segment-tree', 'difference-array'],
  '# lazy[] holds pending range-add; push down before recursing,\n# pull up (t[i]=t[2i]+t[2i+1]) after. Use for range-add range-sum.'),
 ('data-structures',
  'sparse-table-rmq',
  'Sparse Table (RMQ)',
  'O(1) idempotent range min/max after O(n log n) build (static).',
  ['segment-tree', 'binary-search'],
  'def build(a):\n'
  '    sp=[a[:]]; j=1\n'
  '    while (1<<j)<=len(a):\n'
  '        prev=sp[-1]; sp.append([min(prev[i],prev[i+(1<<(j-1))]) for i in range(len(a)-(1<<j)+1)]); j+=1\n'
  '    return sp\n'
  'def qmin(sp,l,r):\n'
  '    j=(r-l+1).bit_length()-1\n'
  '    return min(sp[j][l],sp[j][r-(1<<j)+1])'),
 ('data-structures',
  'monotonic-stack',
  'Monotonic Stack',
  'Next greater/smaller element in O(n).',
  ['heap-priority-queue'],
  'def next_greater(a):\n    res=[-1]*len(a); st=[]\n    for i,x in enumerate(a):\n        while st and a[st[-1]]<x: res[st.pop()]=x\n        st.append(i)\n    return res'),
 ('data-structures',
  'ordered-set',
  'Ordered Set (SortedList)',
  'O(log n) insert + rank/bisect when you need a balanced set.',
  ['binary-search', 'fenwick-bit'],
  'from sortedcontainers import SortedList\nsl=SortedList(); sl.add(x); k=sl.bisect_left(x)  # rank'),
 ('geometry',
  'cross-product-orientation',
  'Cross Product / Orientation',
  'Sign tells CCW/CW/collinear; basis of all 2D geometry.',
  ['convex-hull', 'point-in-polygon'],
  'def cross(o,a,b): return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])\n# >0 ccw, <0 cw, 0 collinear'),
 ('geometry',
  'convex-hull',
  'Convex Hull (monotone chain)',
  'Hull of points in O(n log n).',
  ['cross-product-orientation', 'sorting-tricks'],
  'def cross(o,a,b): return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])\n'
  'def hull(pts):\n'
  '    pts=sorted(set(pts))\n'
  '    if len(pts)<=1: return pts\n'
  '    def half(P):\n'
  '        h=[]\n'
  '        for p in P:\n'
  '            while len(h)>=2 and cross(h[-2],h[-1],p)<=0: h.pop()\n'
  '            h.append(p)\n'
  '        return h\n'
  '    return half(pts)[:-1]+half(pts[::-1])[:-1]'),
 ('geometry',
  'point-in-polygon',
  'Point in Polygon (ray cast)',
  'Parity of crossings of a rightward ray.',
  ['cross-product-orientation'],
  'def inside(poly,x,y):\n'
  '    n=len(poly); c=False; j=n-1\n'
  '    for i in range(n):\n'
  '        xi,yi=poly[i]; xj,yj=poly[j]\n'
  '        if (yi>y)!=(yj>y) and x<(xj-xi)*(y-yi)/(yj-yi)+xi: c=not c\n'
  '        j=i\n'
  '    return c'),
 ('misc',
  'bit-manipulation',
  'Bit Manipulation',
  'Subsets, parity, power-of-two, lowest set bit, popcount.',
  ['combinations-mod', 'dp-bitmask'],
  "x&1; x&(x-1); x&-x; bin(x).count('1')\nfor m in range(1<<n):\n    sub=m\n    while sub: sub=(sub-1)&m  # iterate submasks"),
 ('misc',
  'recursion-backtracking',
  'Backtracking Template',
  'Build incrementally, undo on return.',
  ['bit-manipulation', 'dp-fibonacci-memo'],
  'def bt(path,choices,res):\n    if not choices: res.append(path[:]); return\n    for i,c in enumerate(choices):\n        path.append(c); bt(path,choices[:i]+choices[i+1:],res); path.pop()'),
 ('misc',
  'ternary-search',
  'Ternary Search',
  'Optimum of a unimodal function.',
  ['binary-search'],
  'def tsearch(f,lo,hi):\n    for _ in range(200):\n        m1=lo+(hi-lo)/3; m2=hi-(hi-lo)/3\n        if f(m1)<f(m2): lo=m1\n        else: hi=m2\n    return (lo+hi)/2'),
 ('misc',
  'meet-in-the-middle',
  'Meet in the Middle',
  'Split into halves, combine; turns 2^n into 2^(n/2).',
  ['dp-subset-sum', 'binary-search'],
  '# enumerate all subset sums of each half, sort one, binary-search the other'),
 ('misc',
  'game-theory-nim',
  'Game Theory: Nim / Grundy',
  'First player wins iff XOR of pile sizes != 0; Grundy = mex of moves.',
  ['bit-manipulation'],
  'def nim_win(piles):\n    x=0\n    for p in piles: x^=p\n    return x!=0'),
 ('misc',
  'fast-io-python',
  'Fast I/O in Python',
  'Read all stdin at once for big inputs.',
  ['common-pitfalls'],
  'import sys\ndata=sys.stdin.buffer.read().split(); idx=0\ndef nxt():\n    global idx; v=data[idx]; idx+=1; return v\nt=int(nxt())'),
 ('misc',
  'common-pitfalls',
  'Common Pitfalls',
  'Test-case loop, edge sizes, ceil-div, O(n^2) blowups.',
  ['fast-io-python', 'binary-search'],
  "# ceil division: (a+b-1)//b\n# Python ints don't overflow; still mod when asked\n# for _ in range(int(input())): handle each test case")]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--vault",default=_default_vault()); args=ap.parse_args()
    root=os.path.join(args.vault,"knowledge","algorithms"); os.makedirs(root,exist_ok=True)
    slugs={n[1] for n in NOTES}
    for cat,slug,title,summary,related,code in NOTES:
        links=" ".join("[["+r+"]]" for r in related if r in slugs)
        body=("---\ntype: reference\ntags: reference,algorithm,"+cat+",competitive\n---\n\n"
              "# "+title+"\n\n_Category: "+cat+"_\n\n"+summary+"\n\n```python\n"+code+"\n```\n\n"
              "See also: "+links+" [[algorithms-index]]\n")
        open(os.path.join(root,slug+".md"),"w",encoding="utf-8").write(body)
    by={}
    for cat,slug,title,*_ in NOTES: by.setdefault(cat,[]).append((slug,title))
    idx=["---\ntype: index\ntags: reference,algorithm,index\n---\n","# Algorithms & Programming-Math Index\n",
         "Reference toolkit for competitive / code-gen problems ("+str(len(NOTES))+" notes).\n"]
    for cat in sorted(by):
        idx.append("\n## "+cat+"\n")
        for slug,title in by[cat]: idx.append("- [["+slug+"]] - "+title)
    open(os.path.join(root,"algorithms-index.md"),"w",encoding="utf-8").write("\n".join(idx)+"\n")
    print("wrote",len(NOTES),"reference notes + index ->",root)

if __name__=="__main__": raise SystemExit(main())
