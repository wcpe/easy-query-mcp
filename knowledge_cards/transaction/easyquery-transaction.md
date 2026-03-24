---
card_id: card-easyquery-transaction
title: EasyQuery Transaction
slug: easyquery-transaction
category: transaction
tags: easyquery, transaction, database, rollback, optimistic-lock
keywords: transaction, commit, rollback, optimistic lock, for update, atomicity, retry
aliases: easyquery transaction, orm transaction, repository transaction, 数据库事务
when_to_use: 当一次业务操作要跨多条 SQL 或多张表，并且必须整体成功或整体失败时
avoid: 长事务、事务中混入外部网络 IO、把重试和事务边界混在一起
best_practices: 保持事务短小、并发写入优先明确乐观锁或 for update、先写回归测试再调事务边界
related_symbols: transaction, onConflictThen, forUpdate, executeRows
related_docs: ability/transaction.md, ability/update.md, ability/insertOrUpdate.md
related_examples: sql-test/src/main/java/com/easy/query/test/UpdateTest.java, sql-test/src/main/java/com/easy/query/test/InsertOrUpdateTest.java
source_type: manual
priority: 85
---

# EasyQuery Transaction

EasyQuery 的事务能力适合放在“明确边界的仓储 / 应用服务”里使用，而不是散落在命令层、控制层或回调层里随手拼接。

## 核心结论

- 事务块要只包数据库状态变更，不要把 RPC、玩家消息、缓存广播一起塞进去
- 并发写入场景先判断是“乐观锁重试”还是“`forUpdate()` 串行化”
- `upsert`、批量更新、条件更新都要先确认返回值语义，再决定是否重试
- 失败重试属于事务外层策略，不要在同一个事务块里无限重跑

## 推荐模式

```kotlin
database.transaction {
    val account = query<Account>()
        .where { this.playerId eq playerId }
        .forUpdate()
        .first() ?: return@transaction false

    if (account.balance < cost) {
        return@transaction false
    }

    update<Account>()
        .where { this.playerId eq playerId }
        .set { this.balance to account.balance - cost }
        .executeRows()

    insert<ConsumeLog>(ConsumeLog(playerId, cost))
    true
}
```

## 审查重点

- 是否把事务放在服务层，而不是命令 / HTTP / 监听器入口层
- 是否在事务里做了外部调用
- 是否依赖默认更新行为却没检查 null / 全列更新风险
- 是否把“重试”写进事务块内部，导致锁时间变长

## 相关证据入口

- `sql-core/.../Filterable1.java`
- `sql-test/.../UpdateTest.java`
- `sql-test/.../InsertOrUpdateTest.java`
- `easy-query-doc/src/ability/transaction.md`
- `easy-query-doc/src/ability/update.md`
- `easy-query-doc/src/ability/insertOrUpdate.md`

