import json

ruby = [
 dict(task_id="rb/roman_to_int", language="ruby", difficulty="med",
   prompt="Define method `roman_to_int(s)` converting a Roman numeral string to an Integer (subtractive notation).",
   entry_point="roman_to_int",
   reference=r'''
def roman_to_int(s)
  m={"I"=>1,"V"=>5,"X"=>10,"L"=>50,"C"=>100,"D"=>500,"M"=>1000}
  t=0
  s.chars.each_cons(2){|a,b| m[a]<m[b] ? t-=m[a] : t+=m[a]}
  t + m[s[-1]]
end''',
   test=r'''raise "1" unless roman_to_int("III")==3
raise "2" unless roman_to_int("IV")==4
raise "3" unless roman_to_int("IX")==9
raise "4" unless roman_to_int("LVIII")==58
raise "5" unless roman_to_int("MCMXCIV")==1994'''),

 dict(task_id="rb/group_anagrams", language="ruby", difficulty="med",
   prompt="Define `group_anagrams(words)` returning an Array of groups (each an Array) of words that are anagrams. Order within/among groups does not matter; compare as sets.",
   entry_point="group_anagrams",
   reference=r'''
def group_anagrams(words)
  words.group_by{|w| w.chars.sort.join}.values
end''',
   test=r'''
def norm(g); g.map{|x| x.sort}.sort; end
got = group_anagrams(["eat","tea","tan","ate","nat","bat"])
want = [["eat","tea","ate"],["tan","nat"],["bat"]]
raise "anagram" unless norm(got)==norm(want)'''),

 dict(task_id="rb/valid_parens", language="ruby", difficulty="easy",
   prompt="Define `valid_parens(s)` returning true iff brackets ()[]{} are balanced and correctly nested.",
   entry_point="valid_parens",
   reference=r'''
def valid_parens(s)
  pairs={")"=>"(","]"=>"[","}"=>"{"}
  st=[]
  s.each_char do |c|
    if "([{".include?(c) then st.push(c)
    elsif pairs[c] then return false if st.pop!=pairs[c] end
  end
  st.empty?
end''',
   test=r'''raise "1" unless valid_parens("()[]{}")==true
raise "2" unless valid_parens("(]")==false
raise "3" unless valid_parens("([)]")==false
raise "4" unless valid_parens("{[]}")==true
raise "5" unless valid_parens("")==true'''),

 dict(task_id="rb/rle", language="ruby", difficulty="med",
   prompt="Define `rle(s)` run-length-encoding a string: 'aaabbc' -> 'a3b2c1'. Single chars still get a count of 1.",
   entry_point="rle",
   reference=r'''
def rle(s)
  s.chars.chunk_while{|a,b| a==b}.map{|r| "#{r[0]}#{r.size}"}.join
end''',
   test=r'''raise "1" unless rle("aaabbc")=="a3b2c1"
raise "2" unless rle("abc")=="a1b1c1"
raise "3" unless rle("")==""
raise "4" unless rle("zzzz")=="z4"'''),

 dict(task_id="rb/flatten", language="ruby", difficulty="med",
   prompt="Define `deep_flatten(arr)` returning a single-level Array with all nested elements in order, WITHOUT using Array#flatten.",
   entry_point="deep_flatten",
   reference=r'''
def deep_flatten(arr)
  out=[]
  arr.each{|e| e.is_a?(Array) ? out.concat(deep_flatten(e)) : out.push(e)}
  out
end''',
   test=r'''raise "1" unless deep_flatten([1,[2,[3,[4]],5]])==[1,2,3,4,5]
raise "2" unless deep_flatten([])==[]
raise "3" unless deep_flatten([[1],[2],[3]])==[1,2,3]
raise "4" unless deep_flatten([1,[],[2,[]],3])==[1,2,3]'''),

 dict(task_id="rb/merge_intervals", language="ruby", difficulty="hard",
   prompt="Define `merge_intervals(iv)` where iv is an Array of [start,end] pairs; return merged non-overlapping intervals sorted by start.",
   entry_point="merge_intervals",
   reference=r'''
def merge_intervals(iv)
  return [] if iv.empty?
  s=iv.sort_by{|a| a[0]}
  res=[s[0].dup]
  s[1..].each do |a|
    if a[0]<=res[-1][1] then res[-1][1]=[res[-1][1],a[1]].max
    else res.push(a.dup) end
  end
  res
end''',
   test=r'''raise "1" unless merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]
raise "2" unless merge_intervals([[1,4],[4,5]])==[[1,5]]
raise "3" unless merge_intervals([])==[]
raise "4" unless merge_intervals([[1,4],[0,4]])==[[0,4]]'''),

 dict(task_id="rb/lru_cache", language="ruby", difficulty="hard",
   prompt="Define a class `LRUCache` with `initialize(capacity)`, `get(key)` (returns value or nil, marks most-recently-used), and `put(key,val)` (evicts least-recently-used when over capacity).",
   entry_point="LRUCache",
   reference=r'''
class LRUCache
  def initialize(cap); @cap=cap; @h={}; end
  def get(k)
    return nil unless @h.key?(k)
    v=@h.delete(k); @h[k]=v; v
  end
  def put(k,v)
    @h.delete(k)
    @h[k]=v
    @h.delete(@h.keys.first) if @h.size>@cap
  end
end''',
   test=r'''c=LRUCache.new(2)
c.put(1,1); c.put(2,2)
raise "g1" unless c.get(1)==1
c.put(3,3)            # evicts 2
raise "e2" unless c.get(2).nil?
c.put(4,4)            # evicts 1
raise "e1" unless c.get(1).nil?
raise "g3" unless c.get(3)==3
raise "g4" unless c.get(4)==4'''),
]

# Postgres/SQL — schema+seed portable across sqlite (self-check) and postgres (real run).
pg = [
 dict(task_id="pg/second_highest", language="postgres", difficulty="med", order_matters=False,
   prompt="Return a single column `sec` = the second-highest DISTINCT salary from employees, or NULL if none.",
   schema="CREATE TABLE employees(id INTEGER, salary INTEGER);"
          "INSERT INTO employees VALUES (1,100),(2,200),(3,200),(4,300);",
   reference="SELECT MAX(salary) AS sec FROM employees WHERE salary < (SELECT MAX(salary) FROM employees)"),

 dict(task_id="pg/running_total", language="postgres", difficulty="med", order_matters=True,
   prompt="From txns(acct,day,amt) return acct, day, and a running total `rt` of amt per acct ordered by day. Order rows by acct, day.",
   schema="CREATE TABLE txns(acct TEXT, day INTEGER, amt INTEGER);"
          "INSERT INTO txns VALUES ('a',1,10),('a',2,20),('a',3,5),('b',1,7),('b',2,3);",
   reference="SELECT acct, day, SUM(amt) OVER (PARTITION BY acct ORDER BY day) AS rt FROM txns ORDER BY acct, day"),

 dict(task_id="pg/top_n_per_group", language="postgres", difficulty="hard", order_matters=True,
   prompt="From scores(player,game,pts) return the single highest-pts row per game (ties: lowest player alphabetically). Columns game, player, pts ordered by game.",
   schema="CREATE TABLE scores(player TEXT, game TEXT, pts INTEGER);"
          "INSERT INTO scores VALUES ('ann','x',5),('bob','x',9),('cid','x',9),('ann','y',3),('bob','y',1);",
   reference="SELECT game, player, pts FROM (SELECT player,game,pts, ROW_NUMBER() OVER (PARTITION BY game ORDER BY pts DESC, player ASC) rn FROM scores) t WHERE rn=1 ORDER BY game"),

 dict(task_id="pg/gaps_islands", language="postgres", difficulty="hard", order_matters=True,
   prompt="logins(uid,d) has integer day numbers. For uid='u' return the length `len` of each maximal run of consecutive days, ordered by the run's first day.",
   schema="CREATE TABLE logins(uid TEXT, d INTEGER);"
          "INSERT INTO logins VALUES ('u',1),('u',2),('u',3),('u',5),('u',6),('u',9);",
   reference="SELECT COUNT(*) AS len FROM (SELECT d, d - ROW_NUMBER() OVER (ORDER BY d) AS grp FROM logins WHERE uid='u') t GROUP BY grp ORDER BY MIN(d)"),

 dict(task_id="pg/moving_avg", language="postgres", difficulty="hard", order_matters=True,
   prompt="From series(t,v) return t and the 3-point trailing moving average `ma` (current and up to 2 prior rows by t). Order by t. ma is a real number.",
   schema="CREATE TABLE series(t INTEGER, v INTEGER);"
          "INSERT INTO series VALUES (1,3),(2,6),(3,9),(4,12);",
   reference="SELECT t, AVG(v*1.0) OVER (ORDER BY t ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS ma FROM series ORDER BY t"),

 dict(task_id="pg/dedup_latest", language="postgres", difficulty="med", order_matters=True,
   prompt="events(uid,ts,status) may have many rows per uid. Return the latest (max ts) status per uid as columns uid,status ordered by uid.",
   schema="CREATE TABLE events(uid TEXT, ts INTEGER, status TEXT);"
          "INSERT INTO events VALUES ('a',1,'new'),('a',3,'done'),('a',2,'wip'),('b',1,'new');",
   reference="SELECT uid, status FROM (SELECT uid,status, ROW_NUMBER() OVER (PARTITION BY uid ORDER BY ts DESC) rn FROM events) t WHERE rn=1 ORDER BY uid"),

 dict(task_id="pg/no_orders", language="postgres", difficulty="easy", order_matters=True,
   prompt="Return names of customers(id,name) that have NO row in orders(cid). Column name, ordered by name.",
   schema="CREATE TABLE customers(id INTEGER, name TEXT); CREATE TABLE orders(oid INTEGER, cid INTEGER);"
          "INSERT INTO customers VALUES (1,'ann'),(2,'bob'),(3,'cid');"
          "INSERT INTO orders VALUES (10,1),(11,1),(12,3);",
   reference="SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.cid=c.id) ORDER BY name"),

 dict(task_id="pg/mom_growth", language="postgres", difficulty="hard", order_matters=True,
   prompt="rev(mon,amt) has monthly revenue (mon is 1..N). Return mon and month-over-month delta `d` (amt minus previous month's amt; NULL for first). Order by mon.",
   schema="CREATE TABLE rev(mon INTEGER, amt INTEGER);"
          "INSERT INTO rev VALUES (1,100),(2,150),(3,120);",
   reference="SELECT mon, amt - LAG(amt) OVER (ORDER BY mon) AS d FROM rev ORDER BY mon"),
]

with open("datasets/ruby.jsonl","w") as f:
    for p in ruby: f.write(json.dumps(p)+"\n")
with open("datasets/postgres.jsonl","w") as f:
    for p in pg: f.write(json.dumps(p)+"\n")
print(f"ruby: {len(ruby)} problems, postgres: {len(pg)} problems")
