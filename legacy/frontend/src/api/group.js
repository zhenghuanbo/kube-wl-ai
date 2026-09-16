import { apiGet, apiPost } from './client'

export async function getGroups(params = {}) {
  const res = await apiGet('/group', params)
  return res.data.result || []
}

export async function createGroup(data) {
  const res = await apiPost('/group', data)
  return res.data
}
