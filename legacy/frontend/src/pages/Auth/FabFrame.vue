<template>
  <iframe :src="url" style="width:100%;height:100%;border:0"></iframe>
</template>

<script>
export default {
  name: 'FabFrame',
  props: {
    url: {
      type: String,
      required: false,
      default: ''
    }
  }
}
</script>
