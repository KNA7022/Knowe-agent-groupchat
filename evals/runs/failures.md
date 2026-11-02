# 固定 v1 真实模型逐例失败报告

按每个场景最后一次实际结果列出，旧尝试仍保留于 live-qwen.jsonl。未改变预期或放宽比较；这是模型行为评测，不是桌面端到端验收。

场景数 120；未通过 32。费用总计另见 cost-final.json。

## knowledge-003 · development

失败字段：answer

固定预期：
```json
{
  "answer": "乘积",
  "source_id": "synthetic-03",
  "page": 3
}
```
实际输出：
```json
{
  "answer": "各自概率的乘积",
  "source_id": "synthetic-03",
  "page": 3
}
```

## knowledge-006 · development

失败字段：answer

固定预期：
```json
{
  "answer": "2",
  "source_id": "synthetic-06",
  "page": 6
}
```
实际输出：
```json
{
  "answer": "两个",
  "source_id": "synthetic-06",
  "page": 6
}
```

## knowledge-011 · development

失败字段：answer

固定预期：
```json
{
  "answer": "朝向",
  "source_id": "synthetic-11",
  "page": 11
}
```
实际输出：
```json
{
  "answer": "朝向法线",
  "source_id": "synthetic-11",
  "page": 11
}
```

## learning-002 · development

失败字段：allocated_minutes, unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 25,
  "unallocated_minutes": 35,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 35,
  "unallocated_minutes": 25,
  "conflict": true
}
```

## learning-003 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 40,
  "conflict": false
}
```

## learning-006 · development

失败字段：allocated_minutes, unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 45,
  "unallocated_minutes": 75,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 55,
  "unallocated_minutes": 65,
  "conflict": true
}
```

## learning-007 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 60,
  "conflict": false
}
```

## learning-008 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 45,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 45,
  "unallocated_minutes": 20,
  "conflict": false
}
```

## learning-009 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 60,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 60,
  "unallocated_minutes": 10,
  "conflict": false
}
```

## learning-010 · development

失败字段：allocated_minutes, unallocated_minutes, conflict

固定预期：
```json
{
  "allocated_minutes": 65,
  "unallocated_minutes": 10,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 75,
  "unallocated_minutes": 0,
  "conflict": false
}
```

## learning-011 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 80,
  "conflict": false
}
```

## learning-012 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 85,
  "unallocated_minutes": 20,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 85,
  "unallocated_minutes": 0,
  "conflict": true
}
```

## learning-013 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 90,
  "unallocated_minutes": 30,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 90,
  "unallocated_minutes": 0,
  "conflict": true
}
```

## learning-014 · development

失败字段：allocated_minutes, unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 85,
  "unallocated_minutes": 50,
  "conflict": true
}
```
实际输出：
```json
{
  "allocated_minutes": 95,
  "unallocated_minutes": 40,
  "conflict": true
}
```

## learning-015 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 100,
  "conflict": false
}
```

## learning-016 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 60,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 60,
  "unallocated_minutes": 45,
  "conflict": false
}
```

## learning-017 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 75,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 75,
  "unallocated_minutes": 35,
  "conflict": false
}
```

## learning-018 · development

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 90,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 90,
  "unallocated_minutes": 25,
  "conflict": false
}
```

## protocol-001 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```

## protocol-002 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/messages",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "messages",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```

## protocol-003 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```
实际输出：
```json
{
  "endpoint": "responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```

## protocol-008 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/messages",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "messages",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```

## protocol-009 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```
实际输出：
```json
{
  "endpoint": "responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```

## protocol-010 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```

## protocol-012 · development

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```
实际输出：
```json
{
  "endpoint": "responses",
  "input_key": "input",
  "max_output_key": "max_output_tokens"
}
```

## learning-019 · holdout

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 120,
  "conflict": false
}
```

## learning-020 · holdout

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 120,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 120,
  "unallocated_minutes": 5,
  "conflict": false
}
```

## learning-022 · holdout

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 45,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 45,
  "unallocated_minutes": 90,
  "conflict": false
}
```

## learning-023 · holdout

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 0,
  "unallocated_minutes": 140,
  "conflict": false
}
```

## learning-024 · holdout

失败字段：unallocated_minutes

固定预期：
```json
{
  "allocated_minutes": 75,
  "unallocated_minutes": 0,
  "conflict": false
}
```
实际输出：
```json
{
  "allocated_minutes": 75,
  "unallocated_minutes": 70,
  "conflict": false
}
```

## protocol-013 · holdout

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```

## protocol-016 · holdout

失败字段：endpoint

固定预期：
```json
{
  "endpoint": "/chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
实际输出：
```json
{
  "endpoint": "chat/completions",
  "input_key": "messages",
  "max_output_key": "max_tokens"
}
```
