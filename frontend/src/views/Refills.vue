<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const data = ref<any>(null)
const orders = ref<any[]>([])
async function loadOrders() { orders.value = await api('/refills?location_id=1') }
async function run() {
  data.value = await api('/refills/run?location_id=1', { method: 'POST' })
  await loadOrders()
}
onMounted(loadOrders)
</script>
<template>
  <h1>补货小票</h1>
  <p class="sub">gap = 容量 − 库存 − 在途 · 收据纸样式 · 每次生成按当时货道现算，各落一张完整单</p>
  <button class="btn" @click="run">生成补货单</button>
  <div class="vf-cols">
    <div style="margin-top:1rem" v-if="data">
      <div class="vf-receipt">
        <h2>*** VendFill 补货单 ***</h2>
        <div class="vf-receipt-line" style="font-weight:700;border-bottom:2px dashed #8a7e64">
          <span>货道 / 商品</span><span>补量</span>
        </div>
        <div class="vf-receipt-line" v-for="l in data.lines" :key="l.lane_id">
          <span>{{ l.slot_no }} {{ l.sku_name }}
            <small>({{ l.status === 'need_fill' ? '待补' : l.status === 'full' ? '满仓' : '超占' }})</small>
          </span>
          <span>{{ l.fill_qty }} / 缺{{ l.gap }}</span>
        </div>
        <p style="text-align:center;margin:1rem 0 0;font-size:0.72rem;color:#6a5e48">谢谢使用 · 请核对后装机</p>
      </div>
    </div>
    <div class="card" style="margin-top:1rem;align-self:flex-start">
      <h2 style="font-size:1rem;margin:0 0 .5rem">补货单列表</h2>
      <p v-if="!orders.length" class="muted" style="margin:.25rem 0">暂无已提交的补货单（查看不会自动生成）</p>
      <table v-else>
        <thead><tr><th>#</th><th>生成时间</th><th>总补件数</th><th>待补/满仓/超占</th></tr></thead>
        <tbody>
          <tr v-for="o in orders" :key="o.id">
            <td>{{ o.id }}</td>
            <td>{{ o.created_at }}</td>
            <td>{{ o.total_fill }}</td>
            <td>{{ o.need_fill_count }} / {{ o.full_count }} / {{ o.overbooked_count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.vf-cols { display: flex; gap: 1.5rem; align-items: flex-start; flex-wrap: wrap; }
</style>
