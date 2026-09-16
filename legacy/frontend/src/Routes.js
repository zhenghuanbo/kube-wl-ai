import Vue from 'vue';
import Router from 'vue-router';

import Layout from '@/components/Layout/Layout';

// Pages
import Dashboard from '@/pages/Dashboard/Dashboard';
import Applications from '@/pages/Applications/Typography'
import Instances from '@/pages/Instances/Basic'
import Tasks from '@/pages/Tasks/Notifications'
import Error from "@/pages/Error/Error"
import Login from "@/pages/Login/Login"
import Groups from '@/pages/Admin/Group.vue'
import Users from '@/pages/Admin/User.vue'


import Icons from '@/pages/Elements/Icons/Icons'
import Charts from '@/pages/Elements/Charts/Charts'
//rt Maps from '@/pages/Elements/Maps/Google'

import FabFrame from '@/pages/Auth/FabFrame.vue' // FAB 自带页面

Vue.use(Router);

const router = new Router({
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: Login
    },
    {
    path: '/',
    redirect: 'dashboard',
    name: 'Layout',
    component: Layout,
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: Dashboard,
      },
      {
        path: 'applications',
        name: '个人应用',
        component: Applications,
      },
      {
        path: 'instances',
        name: '容器实例',
        component: Instances
      },
      {
        path: 'tasks',
        name: '自定义任务',
        component: Tasks
      },
      {
        path: 'icons',
        name: 'Icons',
        component: Icons
      },
      {
        path: 'charts',
        name: 'Charts',
        component: Charts
      },
      {
        path: '/admin/users',
        name: 'Users',
        component: Users
      },
      {
        path: '/admin/groups',
        name: 'Groups',
        component: Groups
      },
      {
        path: '/fab-security/:subpath*',
        name: '用户列表',
        component: FabFrame,
        props: route => ({
          url: `http://localhost:5000/${route.params.subpath || ''}`
        })
      },
      {
        path: '/fab-security/:subpath*',
        name: '用户列表',
        component: FabFrame,
        props: route => ({
          url: `http://localhost:5000/${route.params.subpath || ''}`
        })
      },
    ],
  },
    {
      path: '*',
      name: 'Error',
      component: Error
    }
  ],
});

router.beforeEach((to, from, next) => {
  if (to.path !== '/login') {
    fetch('http://localhost:5000/auth/is_authenticated/', {
      credentials: 'include'
    })
      .then(res => res.json())
      .then(data => {
        if (data.username) {
          // 存储username到localStorage
          localStorage.setItem('username', data.username);
          next();
        } else {
          // 跳转到后端登录页，next参数为/jump?next=前端完整地址
          const jumpUrl = 'http://localhost:5000/auth/jump?next=' + encodeURIComponent(window.location.href);
          window.location.href = 'http://localhost:5000/login/?next=' + encodeURIComponent(jumpUrl);
        }
      })
      .catch(() => {
        const jumpUrl = 'http://localhost:5000/auth/jump?next=' + encodeURIComponent(window.location.href);
        window.location.href = 'http://localhost:5000/login/?next=' + encodeURIComponent(jumpUrl);
      });
  } else {
    next();
  }
});

export default router;
