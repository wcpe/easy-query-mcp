---
card_id: card-h2-concurrency-testing
title: H2 Concurrency Testing
slug: h2-concurrency-testing
category: testing
tags: h2, testing, concurrency, database, easyquery
keywords: H2, in-memory database, concurrent test, DB_CLOSE_DELAY, datasource, connection pool
aliases: h2 testing, easyquery h2, concurrent db test, 并发数据库测试
when_to_use: 当你要为 EasyQuery 的 Repository、事务、锁和并发写入补测试时
avoid: 把 H2 当成和 MySQL 完全一致、单连接串行测试假装并发、没有连接池的伪并发测试
best_practices: 使用连接池、开启 DB_CLOSE_DELAY=-1、并发测试用 barrier 或 latch 控制起跑、最终再用目标数据库补验证
related_symbols: DB_CLOSE_DELAY, HikariDataSource, transactionIsolation
related_docs: performance/page.md, performance/batch.md
related_examples: sql-test/src/main/java/com/easy/query/test/h2/H2DocTest.java, sql-test/src/main/java/com/easy/query/test/ShardingTest.java
source_type: manual
priority: 75
---

# H2 Concurrency Testing

H2 很适合给 EasyQuery 做快速仓储回归，但它只能证明“这套 ORM 写法基本成立”，不能替代目标数据库的最终验收。

## 核心结论

- H2 用于快速回归没问题，但锁行为、隔离级别、字符串比较和方言细节不能完全类比 MySQL
- 并发测试必须有连接池和同步起跑机制，否则只是多线程串行执行
- 如果测试目标是事务边界、乐观锁、批量更新、分片分页，最后仍要补真实数据库验证

## 推荐最小配置

```kotlin
val jdbcUrl = "jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1;MODE=MySQL"
```

```kotlin
val barrier = CyclicBarrier(threadCount)
val results = ConcurrentLinkedQueue<Boolean>()
```

## 审查重点

- 是否忘了 `DB_CLOSE_DELAY=-1`
- 是否使用了连接池
- 是否只验证“没报错”，却没验证最终余额、版本号、写入行数
- 是否把 H2 通过误当成 MySQL 也一定通过

## 相关证据入口

- `sql-test/.../h2/H2DocTest.java`
- `sql-test/.../UpdateTest.java`
- `sql-test/.../ShardingTest.java`
- `knowledge/easyquery-transaction`

