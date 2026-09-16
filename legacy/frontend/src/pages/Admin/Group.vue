<template>
  <div>
    <div style="margin-bottom:16px; display:flex; justify-content:flex-end;">
      <el-button type="primary" @click="openDialog">创建新组</el-button>
    </div>

    <el-table :data="groups" style="width: 100%">
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="name" label="组名" />
      <el-table-column prop="company_type" label="公司类别">
        <template slot-scope="scope">
          <span v-if="scope.row.company_type === 1">内部</span>
          <span v-else-if="scope.row.company_type === 2">关联</span>
          <span v-else-if="scope.row.company_type === 3">外部</span>
          <span v-else>未知</span>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog title="创建组" :visible.sync="dialogVisible">
      <el-form :model="form" ref="formRef" label-width="80px">
        <el-form-item label="组名">
          <el-input v-model="form.name" placeholder="请输入组名"></el-input>
        </el-form-item>
        <el-form-item label="公司类别">
          <el-select v-model="form.company_type" placeholder="选择公司类别">
            <el-option label="内部" :value="1"></el-option>
            <el-option label="关联" :value="2"></el-option>
            <el-option label="外部" :value="3"></el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <div slot="footer" class="dialog-footer">
        <el-button @click="dialogVisible=false">取消</el-button>
        <el-button type="primary" :loading="loading" @click="handleCreate">创建</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script>
import { getGroups, createGroup } from '@/api/group'

export default {
  name: 'GroupsPage',
  data() {
    return {
      groups: [],
      dialogVisible: false,
      loading: false,
      form: {
        name: '',
        company_type: 1,
      },
    }
  },
  mounted() {
    this.fetchGroups()
  },
  methods: {
    openDialog() {
      this.form = { name: '', company_type: 1 }
      this.dialogVisible = true
    },
    async fetchGroups() {
      try {
        const res = await getGroups()
        this.groups = res || []
      } catch (e) {
        this.$message && this.$message.error('获取组列表失败')
        console.error(e)
      }
    },
    async handleCreate() {
      if (!this.form.name) {
        this.$message && this.$message.warning('请填写组名')
        return
      }
      this.loading = true
      try {
        await createGroup(this.form)
        this.$message && this.$message.success('创建成功')
        this.dialogVisible = false
        this.fetchGroups()
      } catch (e) {
        this.$message && this.$message.error('创建失败')
        console.error(e)
      } finally {
        this.loading = false
      }
    },
  },
}
</script>

<style scoped>
</style>
