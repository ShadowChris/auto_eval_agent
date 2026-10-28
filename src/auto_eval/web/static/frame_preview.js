import { ref, computed, watch, nextTick, onUnmounted } from "https://unpkg.com/vue@3/dist/vue.esm-browser.js";

export const FramePreview = {
  props: { taskId: String, itemIndex: Number },
  setup(props) {
    const dialog = ref(null);
    const manifest = ref(null);
    const loading = ref(false);
    const error = ref("");
    const imageError = ref(false);
    const imageLoading = ref(false);
    const groupIndex = ref(0);
    const frameIndex = ref(0);
    const knownCount = ref(null);
    let controller = null;
    const group = computed(() => manifest.value?.groups?.[groupIndex.value]);
    const frames = computed(() => group.value?.frames || []);
    const current = computed(() => frames.value[frameIndex.value]);
    const base = computed(() => `/api/eval/${encodeURIComponent(props.taskId || "")}/items/${props.itemIndex}`);

    function dispose() {
      controller?.abort();
      controller = null;
      manifest.value = null;
      loading.value = false;
      error.value = "";
      imageError.value = false;
      imageLoading.value = false;
    }
    function close() { dialog.value?.close(); dispose(); }
    async function open() {
      dispose();
      groupIndex.value = 0;
      frameIndex.value = 0;
      dialog.value?.showModal();
      loading.value = true;
      const request = new AbortController();
      controller = request;
      const timer = setTimeout(() => request.abort(), 30000);
      try {
        const response = await fetch(`${base.value}/frames`, { signal: request.signal, cache: "no-store" });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
        if (controller !== request) return;
        manifest.value = data;
        knownCount.value = data.total_count;
      } catch (exc) {
        if (controller === request) error.value = exc.name === "AbortError" ? "加载超时，请关闭后重试" : (exc.message || "抽帧列表加载失败");
      } finally {
        clearTimeout(timer);
        if (controller === request) loading.value = false;
      }
    }
    function select(index) {
      if (!frames.value.length) return;
      frameIndex.value = (index + frames.value.length) % frames.value.length;
    }
    function keyboard(event) {
      if (["SELECT", "INPUT", "TEXTAREA"].includes(event.target.tagName)) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        select(frameIndex.value + (event.key === "ArrowLeft" ? -1 : 1));
      }
    }
    function timestamp(frame) {
      return typeof frame?.time === "number" ? `${frame.time.toFixed(2)} 秒` : "时间未知";
    }
    function imageLoaded(event, failed = false) {
      if (event.target.dataset.frame !== current.value?.url) return;
      imageLoading.value = false;
      imageError.value = failed;
    }
    watch(groupIndex, () => { frameIndex.value = 0; });
    watch(() => current.value?.url, async () => {
      imageError.value = false;
      imageLoading.value = !!current.value?.available;
      await nextTick();
      dialog.value?.querySelector(".frame-preview-thumb.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    watch(() => `${props.taskId}:${props.itemIndex}`, () => { close(); knownCount.value = null; });
    onUnmounted(close);
    return { dialog, manifest, loading, error, imageError, imageLoading, groupIndex, frameIndex,
      knownCount, group, frames, current, open, close, dispose, select, keyboard, timestamp, imageLoaded };
  },
  template: `
    <span class="frame-preview-entry">
      <button type="button" class="row-export-link frame-preview-trigger" @click.stop="open" :disabled="!taskId">预览<span v-if="knownCount!==null">（{{ knownCount }}张）</span></button>
      <Teleport to="body">
        <dialog ref="dialog" class="frame-preview-dialog" aria-label="录屏抽帧预览" @click.self="close" @close="dispose" @keydown="keyboard">
          <div class="frame-preview-head">
            <div><strong>录屏抽帧预览</strong><span v-if="manifest" class="hint"> · 共 {{ manifest.total_count }} 张</span></div>
            <button type="button" @click="close" aria-label="关闭抽帧预览">关闭</button>
          </div>
          <p v-if="manifest" class="frame-preview-query">{{ manifest.item_id }} · {{ manifest.query }}</p>
          <p v-if="loading" class="frame-preview-message" role="status">正在加载抽帧列表…</p>
          <p v-else-if="error" class="run-error" role="alert">{{ error }}</p>
          <template v-else-if="manifest">
            <label v-if="manifest.groups.length>1" class="frame-preview-group">数据组
              <select v-model.number="groupIndex" aria-label="抽帧数据组"><option v-for="(entry,index) in manifest.groups" :key="index" :value="index">{{ entry.group_name }}（{{ entry.frame_count }}张）</option></select>
            </label>
            <p v-if="!frames.length" class="frame-preview-message">该题或数据组暂无抽帧结果；尚未评估或视频准备失败时不会生成帧。</p>
            <template v-else>
              <div class="frame-preview-stage">
                <button type="button" class="frame-preview-nav" @click="select(frameIndex-1)" :disabled="frames.length<2" aria-label="上一帧">‹</button>
                <div class="frame-preview-image">
                  <span v-if="!current.available || imageError" role="alert">该帧文件已丢失或无法加载，请尝试其他帧。</span>
                  <span v-else-if="imageLoading" class="frame-preview-loading" role="status">正在加载图片…</span>
                  <img v-if="current.available" :key="current.url" :src="current.url" :data-frame="current.url" :alt="'第 '+(frameIndex+1)+' 帧'" :class="{failed:imageError}" @load="imageLoaded" @error="imageLoaded($event,true)">
                </div>
                <button type="button" class="frame-preview-nav" @click="select(frameIndex+1)" :disabled="frames.length<2" aria-label="下一帧">›</button>
              </div>
              <div class="frame-preview-caption"><strong>第 {{ frameIndex+1 }} / {{ frames.length }} 张</strong><span>{{ timestamp(current) }}</span><span>{{ current.filename }}</span><span class="hint">支持键盘 ← → 切换，Esc 关闭</span></div>
              <div class="frame-preview-thumbnails" aria-label="抽帧缩略图">
                <button v-for="(frame,index) in frames" :key="index" type="button" class="frame-preview-thumb" :class="{active:index===frameIndex}" :aria-pressed="index===frameIndex" :aria-label="'查看第 '+(index+1)+' 帧'" @click="select(index)">
                  <img v-if="frame.available" :src="frame.url+'&thumbnail=true'" loading="lazy" alt="" draggable="false">
                  <span v-else class="hint">文件缺失</span>
                  <span>#{{ index+1 }} · {{ timestamp(frame) }}</span>
                </button>
              </div>
            </template>
          </template>
        </dialog>
      </Teleport>
    </span>`,
};
