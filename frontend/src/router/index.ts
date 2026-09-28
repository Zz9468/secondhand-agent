import { createRouter, createWebHistory } from 'vue-router'

import { useAuth } from '../state/auth'
import HomeView from '../views/HomeView.vue'
import LoginView from '../views/LoginView.vue'
import RegisterView from '../views/RegisterView.vue'
import SellerView from '../views/SellerView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { guestOnly: true },
    },
    {
      path: '/register',
      name: 'register',
      component: RegisterView,
      meta: { guestOnly: true },
    },
    {
      path: '/buyer',
      name: 'buyer',
      component: () => import('../views/BuyerView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/buyer/negotiations',
      name: 'buyer-negotiations',
      component: () => import('../views/BuyerNegotiationsView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/sellers/:sellerId',
      name: 'seller-public',
      component: () => import('../views/SellerPublicView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/products/:productId',
      name: 'product-detail',
      component: () => import('../views/ProductDetailView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/negotiations/:sessionId',
      name: 'negotiation',
      component: () => import('../views/NegotiationView.vue'),
      meta: { requiresAuth: true },
    },
    {
      path: '/seller',
      redirect: { name: 'seller-products' },
      meta: { requiresAuth: true },
    },
    {
      path: '/seller/products',
      name: 'seller-products',
      component: SellerView,
      meta: { requiresAuth: true },
    },
    {
      path: '/seller/approvals',
      name: 'seller-approvals',
      component: SellerView,
      meta: { requiresAuth: true },
    },
    {
      path: '/seller/negotiations',
      name: 'seller-negotiations',
      component: SellerView,
      meta: { requiresAuth: true },
    },
    {
      path: '/seller/model-tasks',
      name: 'seller-model-tasks',
      component: SellerView,
      meta: { requiresAuth: true },
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuth()
  try {
    await auth.initialize()
  } catch {
    if (to.meta.requiresAuth) {
      return {
        name: 'login',
        query: { redirect: to.fullPath, reason: 'unavailable' },
      }
    }
  }

  if (to.meta.requiresAuth && auth.currentUser.value === null) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.guestOnly && auth.currentUser.value !== null) {
    return { name: 'home' }
  }
  return true
})

export default router
