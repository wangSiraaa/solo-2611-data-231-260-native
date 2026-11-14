CREATE TABLE fact(id INTEGER PRIMARY KEY,day TEXT NOT NULL,region TEXT,product TEXT NOT NULL,amount INTEGER);
INSERT INTO fact VALUES(1,'2026-01-01','N','A',10),(2,'2026-01-01','N','A',30),(3,'2026-01-01','N','B',NULL),(4,'2026-01-01','S','A',100),(5,'2026-01-02','N','A',20),(6,'2026-01-02','N','B',40),(7,'2026-01-02',NULL,'A',NULL),(8,'2026-01-01','N','A',70);
CREATE TABLE v_detail AS SELECT day,region,product,COUNT(*) AS n,COUNT(amount) AS n_amount,SUM(amount) AS s_amount FROM fact GROUP BY day,region,product;
CREATE TABLE v_daily AS SELECT day,region,COUNT(*) AS n,COUNT(amount) AS n_amount,SUM(amount) AS s_amount FROM fact GROUP BY day,region;
CREATE TABLE v_avg AS SELECT region,AVG(amount) AS avg_amount FROM fact GROUP BY region;
CREATE TABLE v_a AS SELECT day,region,COUNT(*) AS n,COUNT(amount) AS n_amount,SUM(amount) AS s_amount FROM fact WHERE product='A' GROUP BY day,region;
CREATE TABLE v_stale AS SELECT * FROM v_daily WHERE day='2026-01-01';
