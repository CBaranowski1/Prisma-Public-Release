"""Inject a Ruby + PostgreSQL/SQL reference corpus into the brain so retrieval-
augmented answers cover those stacks (not just Python). GENERAL idioms/patterns.
Ruby snippets are ruby -c validated; SQL is Postgres dialect.

  python scripts/build_ruby_sql_corpus.py
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
NOTES=[('ruby',
  'ruby-io-stdin',
  'Ruby: Reading stdin',
  'Competitive I/O: gets for a line, split+map for arrays, a T-loop for multiple test cases.',
  ['ruby-competitive-template'],
  'ruby',
  'n = gets.to_i\na = gets.split.map(&:to_i)\nt = gets.to_i\nt.times do\n  line = gets.chomp\n  puts line\nend'),
 ('ruby',
  'ruby-enumerable',
  'Ruby: Enumerable idioms',
  'map/select/reduce/sort_by/group_by/tally are the workhorses. &:sym is shorthand for a block.',
  ['ruby-hash-counter', 'ruby-sorting'],
  'ruby',
  'a = [3, 1, 2]\np a.map { |x| x * 2 }\np a.select(&:even?)\np a.reduce(0) { |s, x| s + x }\np a.sort_by { |x| -x }\np a.each_with_index.map { |x, i| [i, x] }\np a.group_by(&:odd?)\np a.tally'),
 ('ruby',
  'ruby-hash-counter',
  'Ruby: Hash & counting',
  'Hash.new(0) gives an auto-zero counter. sort_by/max_by rank entries.',
  ['ruby-enumerable'],
  'ruby',
  'counts = Hash.new(0)\n"banana".each_char { |c| counts[c] += 1 }\np counts\np counts.max_by { |_k, v| v }\np counts.sort_by { |k, v| [-v, k] }'),
 ('ruby',
  'ruby-strings',
  'Ruby: String methods',
  'chars/split/gsub/scan/reverse/count/include? cover most string tasks. scan takes a regex.',
  ['ruby-formatting'],
  'ruby',
  's = "hello world"\np s.chars\np s.split\np s.gsub("o", "0")\np s.scan(/\\w+/)\np s.reverse\np s.count("l")\np s.include?("wor")'),
 ('ruby',
  'ruby-blocks-procs',
  'Ruby: Blocks, procs, lambdas',
  'Blocks via { } or do..end; yield calls them; lambdas are first-class and pass with &.',
  ['ruby-enumerable'],
  'ruby',
  'def each_pair(arr)\n  arr.each_slice(2) { |pair| yield pair }\nend\nsquare = ->(x) { x * x }\np square.call(5)\np [1, 2, 3].map(&square)'),
 ('ruby',
  'ruby-ranges-loops',
  'Ruby: Ranges & loops',
  '(a..b).each, n.times, upto/downto, and step for strides.',
  ['ruby-io-stdin'],
  'ruby',
  '(1..5).each { |i| print i }\nputs\n5.times { |i| print i }\nputs\n3.upto(6) { |i| print i }\nputs\n10.step(0, -2) { |i| print i }\nputs'),
 ('ruby',
  'ruby-math',
  'Ruby: Integer math',
  'gcd/lcm are Integer methods; ** is power; / is integer division; Integer.sqrt for isqrt.',
  ['ruby-gotchas'],
  'ruby',
  'p 12.gcd(18)\np 12.lcm(18)\np 2 ** 10\np 17 / 5\np 17 % 5\np 17.fdiv(5)\np Integer.sqrt(50)'),
 ('ruby',
  'ruby-sorting',
  'Ruby: Sorting',
  'sort_by with a key (tuple for multi-key); sort with <=> for custom order. Stable via index.',
  ['ruby-enumerable'],
  'ruby',
  'people = [["ann", 30], ["bob", 25]]\np people.sort_by { |name, age| [age, name] }\np [3, 1, 2].sort { |a, b| b <=> a }\np [3, 1, 2].minmax'),
 ('ruby',
  'ruby-gotchas',
  'Ruby: Common gotchas',
  'Integer / Integer truncates; use fdiv or a float. nil.to_i is 0, nil.to_s is empty.',
  ['ruby-math'],
  'ruby',
  'p 7 / 2\np 7.0 / 2\np 7.fdiv(2)\nx = nil\np x.to_i\np x.to_s\np [1, 2, 3].sum'),
 ('ruby',
  'ruby-data-structures',
  'Ruby: Set / stack / Prime',
  'Set for membership, Array as stack/queue (push/pop/shift), Prime for primality.',
  ['ruby-math'],
  'ruby',
  "require 'set'\ns = Set.new([1, 2, 2, 3])\np s.include?(2)\nstack = []\nstack.push(1); stack.push(2)\np stack.pop\nrequire 'prime'\np Prime.prime?(7)"),
 ('ruby',
  'ruby-competitive-template',
  'Ruby: Fast-IO template',
  'Read all stdin once and index through it for big inputs.',
  ['ruby-io-stdin'],
  'ruby',
  'data = STDIN.read.split("\\n")\nidx = 0\nt = data[idx].to_i; idx += 1\nt.times do\n  n = data[idx].to_i; idx += 1\n  arr = data[idx].split.map(&:to_i); idx += 1\n  puts arr.sum\nend'),
 ('ruby',
  'ruby-formatting',
  'Ruby: Output formatting',
  'printf/format/% for formatted output; join for arrays; p for inspect.',
  ['ruby-strings'],
  'ruby',
  'printf("%05d\\n", 42)\nputs format("%.2f", 3.14159)\nputs("%s is %d" % ["age", 30])\np [1, 2, 3]\nputs [1, 2, 3].join(" ")'),
 ('sql',
  'sql-select-basics',
  'SQL: SELECT basics',
  'Filter (WHERE), order, limit, dedup (DISTINCT). The foundation of every query.',
  ['sql-joins', 'sql-aggregation'],
  'sql',
  "SELECT DISTINCT name, age\nFROM users\nWHERE age >= 18 AND country = 'US'\nORDER BY age DESC, name ASC\nLIMIT 10;"),
 ('sql',
  'sql-joins',
  'SQL: Joins',
  'INNER keeps matches; LEFT keeps all left rows (NULLs on no match). Self-join aliases the table twice.',
  ['sql-select-basics', 'sql-subqueries'],
  'sql',
  'SELECT o.id, u.name, o.total\n'
  'FROM orders o\n'
  'JOIN users u   ON u.id = o.user_id          -- inner\n'
  'LEFT JOIN refunds r ON r.order_id = o.id;      -- keep orders w/o refunds\n'
  '-- self join: employees e JOIN employees m ON m.id = e.manager_id'),
 ('sql',
  'sql-aggregation',
  'SQL: GROUP BY & HAVING',
  'Aggregate per group; WHERE filters rows before grouping, HAVING filters groups after.',
  ['sql-window-functions'],
  'sql',
  "SELECT user_id, COUNT(*) AS n, SUM(total) AS spent, AVG(total) AS avg\nFROM orders\nWHERE status = 'paid'\nGROUP BY user_id\nHAVING SUM(total) > 100\nORDER BY spent DESC;"),
 ('sql',
  'sql-window-functions',
  'SQL: Window functions',
  'Compute across rows without collapsing them: ROW_NUMBER/RANK, running totals, LAG/LEAD.',
  ['sql-aggregation', 'postgres-topn-pagination'],
  'sql',
  'SELECT user_id, total,\n'
  '  ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY total DESC) AS rn,\n'
  '  SUM(total)  OVER (PARTITION BY user_id ORDER BY created_at) AS running,\n'
  '  LAG(total)  OVER (PARTITION BY user_id ORDER BY created_at) AS prev\n'
  'FROM orders;'),
 ('sql',
  'sql-cte',
  'SQL: CTEs (incl. recursive)',
  'WITH names a subquery for readability; RECURSIVE walks hierarchies/sequences.',
  ['sql-subqueries'],
  'sql',
  'WITH paid AS (\n'
  "  SELECT * FROM orders WHERE status = 'paid'\n"
  ')\n'
  'SELECT user_id, COUNT(*) FROM paid GROUP BY user_id;\n'
  '\n'
  'WITH RECURSIVE nums(n) AS (\n'
  '  SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 10\n'
  ')\n'
  'SELECT n FROM nums;'),
 ('sql',
  'sql-subqueries',
  'SQL: Subqueries & EXISTS',
  'Correlated subqueries, EXISTS/IN for membership, scalar subqueries in SELECT.',
  ['sql-joins', 'sql-cte'],
  'sql',
  'SELECT u.name\nFROM users u\nWHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.total > 50)\n  AND u.id IN (SELECT user_id FROM premium);'),
 ('sql',
  'postgres-upsert',
  'Postgres: UPSERT (ON CONFLICT)',
  'Insert-or-update in one statement; EXCLUDED refers to the would-be-inserted row.',
  ['postgres-jsonb'],
  'sql',
  "INSERT INTO counters (key, n)\nVALUES ('hits', 1)\nON CONFLICT (key)\nDO UPDATE SET n = counters.n + EXCLUDED.n\nRETURNING n;"),
 ('sql',
  'postgres-jsonb',
  'Postgres: JSONB',
  '-> keeps JSON, ->> returns text, @> tests containment; GIN-index for fast @>.',
  ['postgres-arrays'],
  'sql',
  "SELECT data->>'name' AS name, data->'addr'->>'city' AS city\n"
  'FROM events\n'
  'WHERE data @> \'{"active": true}\'::jsonb;\n'
  '-- CREATE INDEX ON events USING GIN (data);\n'
  "-- build: SELECT jsonb_build_object('k', 1, 'arr', jsonb_build_array(1,2));"),
 ('sql',
  'postgres-arrays',
  'Postgres: Arrays',
  'Native arrays with ANY/ALL, unnest to rows, array_agg to collect.',
  ['postgres-jsonb'],
  'sql',
  'SELECT * FROM t WHERE 5 = ANY(nums);\nSELECT user_id, array_agg(tag ORDER BY tag) FROM tags GROUP BY user_id;\nSELECT * FROM unnest(ARRAY[1,2,3]) AS x;'),
 ('sql',
  'postgres-generate-series',
  'Postgres: generate_series',
  'Generate number/date ranges - great for calendars, gap-filling, test data.',
  ['sql-cte'],
  'sql',
  "SELECT d::date\nFROM generate_series('2024-01-01'::date, '2024-01-31'::date, '1 day') AS d;\nSELECT n FROM generate_series(1, 100) AS n;"),
 ('sql',
  'postgres-indexes-explain',
  'Postgres: Indexes & EXPLAIN',
  'B-tree for equality/range, GIN for jsonb/arrays/full-text. EXPLAIN ANALYZE shows the real plan.',
  ['postgres-jsonb'],
  'sql',
  'CREATE INDEX idx_orders_user ON orders (user_id);\nCREATE INDEX idx_events_data ON events USING GIN (data);\nEXPLAIN ANALYZE SELECT * FROM orders WHERE user_id = 42;'),
 ('sql',
  'postgres-topn-pagination',
  'Postgres: Top-N & keyset pagination',
  'Top-N-per-group via window filter or DISTINCT ON; keyset paging beats OFFSET at scale.',
  ['sql-window-functions'],
  'sql',
  '-- top 1 order per user:\nSELECT DISTINCT ON (user_id) user_id, id, total\nFROM orders ORDER BY user_id, total DESC;\n-- keyset page (faster than OFFSET):\nSELECT * FROM orders WHERE id > $1 ORDER BY id LIMIT 20;')]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--vault",default=_default_vault()); a=ap.parse_args()
    made={}
    for cat,slug,title,summary,related,lang,code in NOTES:
        root=os.path.join(a.vault,"knowledge",cat); os.makedirs(root,exist_ok=True)
        links=" ".join("[["+r+"]]" for r in related)
        body=("---\ntype: reference\ntags: reference,"+cat+","+lang+",language\n---\n\n# "+title+
              "\n\n"+summary+"\n\n```"+lang+"\n"+code+"\n```\n\nSee also: "+links+" [["+cat+"-index]]\n")
        open(os.path.join(root,slug+".md"),"w",encoding="utf-8").write(body); made.setdefault(cat,[]).append((slug,title))
    for cat,items in made.items():
        root=os.path.join(a.vault,"knowledge",cat)
        idx=["---\ntype: index\ntags: reference,"+cat+",index\n---\n","# "+cat.upper()+" Reference Index\n"]
        for slug,title in items: idx.append("- [["+slug+"]] - "+title)
        open(os.path.join(root,cat+"-index.md"),"w",encoding="utf-8").write("\n".join(idx)+"\n")
    print("wrote", sum(len(v) for v in made.values()), "notes across", list(made), "->", a.vault)
if __name__=="__main__": raise SystemExit(main())
