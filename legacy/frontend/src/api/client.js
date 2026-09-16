import axios from 'axios'

const backendBaseURL = process.env.VUE_APP_API_BASE_URL || 'http://localhost:5000/api/v1'

const client = axios.create({
  baseURL: backendBaseURL,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

export default client

export async function apiGet(path, params = {}) {
  return client.get(path, { params })
}

export async function apiPost(path, data = {}) {
  return client.post(path, data)
}
