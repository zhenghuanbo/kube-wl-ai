<template>
  <v-navigation-drawer
    app
    clipped
    v-model="DRAWER_STATE"
    :mini-variant="!DRAWER_STATE"
    :width="sidebarWidth"
    :permanent="$vuetify.breakpoint.mdAndUp"
    :temporary="$vuetify.breakpoint.smAndDown"
    :mini-variant-width="sidebarMinWidth"
    :class="{'drawer-mini': !DRAWER_STATE}">

<!-- 
    <div class="v-list">
      <a class="v-list-item v-list-item--link grey--text text-sm-h6" target="_blank" >工作台</a>
    </div>
-->
    <v-list>
      <template v-for="(item, i) in items">
        <v-row
          v-if="item.heading"
          :key="item.heading"
          align="center">
          <v-col cols="6" class="py-5">
            <span
              style="padding-left: 32px"
              class="text-body-1 subheader"
              :class="(item.heading && DRAWER_STATE) ? 'show ' : 'hide'">
              {{ item.heading }}
              </span>
          </v-col>
          <v-col
            cols="6"
            class="text-center">
          </v-col>
        </v-row>
        <v-divider
          v-else-if="item.divider"
          :key="i"
          dark
          class="my-4"
        ></v-divider>
        <v-list-group
          color="primary"
          v-else-if="item.children && DRAWER_STATE"
          :key="item.title"
          v-model="item.model"
          append-icon="">
            <template v-slot:prependIcon>
              <v-icon size="28">mdi-image-filter-none</v-icon>
            </template>
            <template v-slot:activator >
              <v-list-item-content >
                <v-list-item-title
                  class="grey--text">
                    {{ item.title }}
                </v-list-item-title>
              </v-list-item-content>
            </template>
            <v-list-item
              v-for="(child, i) in item.children"
              :key="i"
              :to="child.link"
              link>
                <v-list-item-action v-if="child.icon">
                  <v-icon size="">{{ child.icon }}</v-icon>
                </v-list-item-action>
                <v-list-item-content>
                  <v-list-item-title class="grey--text">
                    {{ child.title }}
                  </v-list-item-title>
                </v-list-item-content>
            </v-list-item>
        </v-list-group>
        <v-list-item
          color="primary"
          v-else
          :key="item.text"
          :href="item.href ? item.href : null"
          :to="item.link === '#' ? null : item.link"
          link>
          <v-list-item-action>
            <v-icon
              size="28"
              :color="item.color ? item.color : ''"
            >{{ item.icon }}</v-icon>
          </v-list-item-action>
            <v-list-item-content>
              <v-list-item-title
                class="grey--text"
                link>
                {{ item.title }}
              </v-list-item-title>
            </v-list-item-content>
        </v-list-item>
      </template>
    </v-list>
    </v-navigation-drawer>
</template>

<script>
import {mapActions, mapState} from 'vuex'

  export default {
    props: {
        source: String,
    },
    data(){
      return {
        items: [
          { heading: '工作台' },
          { title: 'Dashboard', icon: 'mdi-home', link: '/dashboard' },
          { title: '个人应用', icon: 'mdi-format-size', link: '/applications' },
          { title: '容器服务器', icon: 'mdi-grid-large', link: '/instances' },
          { title: '自定义任务', icon: 'mdi-bell-outline', link: '/tasks' },
          { divider: true },
          { heading: '资源中心' },
          { title: '镜像', icon: 'mdi-vector-arrange-below', link: '/images'},
          { title: '计算资源', icon: 'mdi-forum', link: '/compute'},
          { title: '存储资源', icon: 'mdi-trello', link: '/volumes'},
          { divider: true },
          { heading: '信息中心' },
          { title: '机时统计', icon: 'mdi-animation', link: '/statistics'},
          { title: '工单', icon: 'mdi-wrench', link: '/workorder'},
          { title: 'K8s-dashboard', icon: 'mdi-alert-box'},
          { title: 'Grafana', icon: 'mdi-alert-box'},
          { divider: true },
          { heading: '管理员' },
          { title: '用户管理', icon: 'mdi-alert-box',link: '/admin/users'},
          { title: '组管理', icon: 'mdi-alert-box',link: '/admin/groups'},
          {
            title: 'fab-security',
            icon: 'mdi-image-filter-none',
            link: '/icons',
            model: false,
            children: [
              { title: 'Icons', icon: 'mdi-circle-small', link: '/icons'},
              { title: 'Charts', icon: 'mdi-circle-small', link: '/charts'},
              { title: 'Maps', icon: 'mdi-circle-small', link: '/maps'},
              { title: '用户列表', icon: 'mdi-account', link: '/fab-security/users/list'},
              { title: '用户组', icon: 'mdi-account-multiple', link: '/fab-security/groups/list'},
              { title: '角色列表', icon: 'mdi-account-multiple', link: '/fab-security/roles/list'},
              { title: '用户统计', icon: 'mdi-account-key', link: '/fab-security/userstatschartview/chart/'},
              { title: '基本权限', icon: 'mdi-account-key', link: '/fab-security/permissions/list/'},
              { title: '视图菜单', icon: 'mdi-key', link: '/fab-security/viewmenus/list/'},
              { title: '权限视图映射', icon: 'mdi-key', link: '/fab-security/permissionviews/list/'},
            ],
          },
          { divider: true },
          { heading: '系统设置' },
          { title: '系统配置', icon: 'mdi-cog', link: '/settings'},
          { title: '日志管理', icon: 'mdi-file-document-box', link: '/logs'},
        ],
        sidebarWidth: 240,
        sidebarMinWidth: 96
      }
    },
    computed: {
      ...mapState(['drawer']),
      DRAWER_STATE: {
        get() {
          return this.drawer
        },
        set(newValue) {
          if (newValue === this.drawer) return;
          this.TOGGLE_DRAWER();
        }
      }
    },
    methods: {
      ...mapActions([ 'TOGGLE_DRAWER' ]),
    }
  }
</script>

<style src="./Sidebar.scss" lang="scss"/>
