"""Lua scripts executed inside Redis.

Redis runs a script as ONE atomic unit: no other command from any client can run between
its lines. That turns "read counters, compare with the limit, then reserve" into a single
indivisible step, which is what prevents two concurrent requests from both seeing
"there is room" and both reserving it.

Numbers: Lua numbers are doubles (exact for integers up to 2^53, i.e. ~$9M in nano-dollars).
We never turn a Lua number back into a string for Redis (Lua would print large values as
"1e+15", which INCRBY rejects). Amounts are passed through as ARGV strings instead.
"""

# KEYS: spent_1, reserved_1, spent_2, reserved_2, ...   (one pair per counter)
# ARGV: amount, allow_over ("1"/"0"), then per counter: ttl_i, limit_i  (limit -1 = unlimited)
# Returns: {allowed (1/0), spent_1, reserved_1, spent_2, reserved_2, ...}
# The spent/reserved values are the ones seen BEFORE this reservation.
RESERVE_LUA = """
local amount = tonumber(ARGV[1])
local allow_over = ARGV[2] == '1'
local n = #KEYS / 2
local result = {0}
local blocked = false

for i = 1, n do
  local spent = tonumber(redis.call('GET', KEYS[2 * i - 1]) or '0')
  local reserved = tonumber(redis.call('GET', KEYS[2 * i]) or '0')
  local limit = tonumber(ARGV[2 + 2 * i])
  if limit >= 0 and spent + reserved + amount >= limit then
    blocked = true
  end
  result[2 * i] = spent
  result[2 * i + 1] = reserved
end

if blocked and not allow_over then
  return result
end

for i = 1, n do
  redis.call('INCRBY', KEYS[2 * i], ARGV[1])
  redis.call('EXPIRE', KEYS[2 * i], ARGV[1 + 2 * i])
end
result[1] = 1
return result
"""

# KEYS: spent_1, reserved_1, spent_2, reserved_2, ...
# ARGV: reserved_amount, actual_amount, then per counter: ttl_i
# Releases the hold and books the real cost, in one atomic step.
SETTLE_LUA = """
local reserved_amount = tonumber(ARGV[1])
local actual = tonumber(ARGV[2])
local n = #KEYS / 2

for i = 1, n do
  local spent_key = KEYS[2 * i - 1]
  local reserved_key = KEYS[2 * i]
  local ttl = ARGV[2 + i]
  local current = tonumber(redis.call('GET', reserved_key) or '0')
  -- Clamp at 0: reconciliation may have reset the hold while this request was in flight
  if current <= reserved_amount then
    redis.call('SET', reserved_key, '0', 'EX', ttl)
  else
    redis.call('DECRBY', reserved_key, ARGV[1])
    redis.call('EXPIRE', reserved_key, ttl)
  end
  if actual > 0 then
    redis.call('INCRBY', spent_key, ARGV[2])
  end
  redis.call('EXPIRE', spent_key, ttl)
end
return 1
"""
