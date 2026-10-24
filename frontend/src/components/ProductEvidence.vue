<script setup lang="ts">
import { ref, watch } from 'vue'
import { requestJson } from '../api/client'

const props = defineProps<{ productId: number }>()
interface Evidence {
  product_version: number
  facts: Record<string, { value: { value: string | string[] | null }; version: number }>
  rules: { id: string; region: string; shipping_cost: string; discount: string; active: boolean; expires_at: string }[]
  verifications: { id: string; draft_id: string; status: string; reasons: string[] }[]
}
const data = ref<Evidence | null>(null)
const battery = ref('')
const repair = ref('UNKNOWN')
const pickup = ref(false)
const shipping = ref(false)
const region = ref('')
const cost = ref('')
const discount = ref('0')
const expires = ref('')
const error = ref('')
const notice = ref('')
const busy = ref(false)
const path = () => `/api/seller/products/${props.productId}`

async function load() {
  const pid = props.productId
  try {
    const result = await requestJson<Evidence>(`${path()}/evidence`)
    if (pid !== props.productId) return
    data.value = result
    battery.value = String(result.facts.battery_health_pct?.value.value ?? '')
    repair.value = String(result.facts.repair_history?.value.value ?? 'UNKNOWN')
    const delivery = result.facts.allowed_delivery_methods?.value.value
    pickup.value = Array.isArray(delivery) && delivery.includes('pickup')
    shipping.value = Array.isArray(delivery) && delivery.includes('shipping')
  } catch (e) { error.value = e instanceof Error ? e.message : '读取资料失败' }
}

async function saveFact(attribute: string, value: string | string[] | null) {
  if (!data.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await requestJson(`${path()}/facts/${attribute}`, { method: 'PUT', body: JSON.stringify({ expected_version: data.value.product_version, value: value === null || Array.isArray(value) ? value : String(value) }) })
    notice.value = '资料已保存；满足条件的草案会通知买家再次确认继续。'
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '保存失败，请刷新后重试' }
  finally { busy.value = false }
}

async function saveRule(old?: Evidence['rules'][number]) {
  if (!data.value || busy.value) return
  if (!old && (!region.value.trim() || cost.value === '' || !expires.value)) { error.value = '请填写区域、运费成本和失效时间'; return }
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const rule = old ? { rule_id: old.id, region: old.region, shipping_cost: old.shipping_cost,
      discount: old.discount, expires_at: old.expires_at.endsWith('Z') ? old.expires_at : `${old.expires_at}Z`, active: false }
      : { region: region.value.trim(), shipping_cost: cost.value, discount: discount.value,
        expires_at: new Date(expires.value).toISOString(), active: true }
    await requestJson(`${path()}/shipping-rules`, { method: 'PUT', body: JSON.stringify({ expected_version: data.value.product_version, ...rule }) })
    notice.value = '配送规则已保存。重叠规则不会用于报价授权。'
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '规则保存失败' }
  finally { busy.value = false }
}

async function recheck() {
  busy.value = true; error.value = ''
  try { await requestJson(`${path()}/verifications/recheck`, { method: 'POST' }); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : '核实失败' }
  finally { busy.value = false }
}

watch(() => props.productId, () => { data.value = null; error.value = ''; notice.value = ''; void load() }, { immediate: true })
</script>

<template>
  <section class="evidence-panel">
    <h2>商品事实与配送核实</h2>
    <p>仅填写你已确认的资料。描述里的推测不会用于条件报价；保存资料不会自动提交买家的旧草案。</p>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <p v-if="notice" class="outcome-banner">{{ notice }}</p>
    <template v-if="data">
      <div class="evidence-row"><label>电池健康（%）<input v-model="battery" type="number" min="0" max="100" step="0.01" placeholder="未知请留空"></label><button type="button" :disabled="busy" @click="saveFact('battery_health_pct', battery || null)">保存电池资料</button></div>
      <div class="evidence-row"><label>维修历史<select v-model="repair"><option value="UNKNOWN">未知</option><option value="NO">未维修</option><option value="YES">已维修</option></select></label><button type="button" :disabled="busy" @click="saveFact('repair_history', repair)">保存维修资料</button></div>
      <div class="evidence-row"><label><input v-model="pickup" type="checkbox">支持自提</label><label><input v-model="shipping" type="checkbox">支持快递</label><button type="button" :disabled="busy || (!pickup && !shipping)" @click="saveFact('allowed_delivery_methods', [...(pickup ? ['pickup'] : []), ...(shipping ? ['shipping'] : [])])">保存配送方式</button></div>
      <h3>区域配送规则</h3><p>成本仅卖家可见。买家承担运费时不表示买家免运费。</p>
      <ul><li v-for="rule in data.rules" :key="rule.id">{{ rule.region }} · 运费成本 ¥{{ rule.shipping_cost }} · 其他优惠成本 ¥{{ rule.discount }} · {{ rule.active ? '已启用' : '已停用' }} <button v-if="rule.active" type="button" :disabled="busy" @click="saveRule(rule)">停用</button></li></ul>
      <div class="evidence-row"><label>区域<input v-model="region" maxlength="80" placeholder="如：上海"></label><label>卖家运费成本<input v-model="cost" type="number" min="0" step="0.01"></label><label>其他优惠成本<input v-model="discount" type="number" min="0" step="0.01"></label><label>失效时间<input v-model="expires" type="datetime-local"></label><button type="button" :disabled="busy" @click="saveRule()">新增规则</button></div>
      <h3>草案核实事项</h3>
      <p v-if="!data.verifications.length">暂无待核实事项</p>
      <ul><li v-for="item in data.verifications" :key="item.id">草案 {{ item.draft_id.slice(0, 8) }}：{{ item.status === 'READY' ? '资料已齐，等待买家继续' : item.status === 'STALE' ? '已失效' : '需要核实' }}<p>{{ item.reasons.map(reason => reason.includes('battery') ? '电池条件待核实' : reason.includes('repair') ? '维修记录待核实' : reason.includes('cost_') ? '区域费用缺失或规则重叠' : reason.includes('delivery') ? '配送方式待确认' : '请核实商品资料与报价前提').join('；') }}</p></li></ul>
      <button type="button" :disabled="busy" @click="recheck">重新核实并通知可继续的草案</button>
    </template>
  </section>
</template>

<style scoped>
.evidence-panel { grid-column: 1 / -1; padding: 24px; border: 1px solid #d7e1dc; border-radius: 16px; background: white; }
.evidence-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 18px 0; }
label { display: flex; flex-direction: column; gap: 6px; }
input, select { padding: 9px; border: 1px solid #b9c7c0; border-radius: 6px; }
button { padding: 9px 14px; }
li { margin: 10px 0; }
</style>
