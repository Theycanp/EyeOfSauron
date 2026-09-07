import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AdminApi, ApiError } from '../shared/api'
import type { AdminResourceName, AdminResources, ConfigResponse, IncidentResponse, NewsCatalogResponse, PromptResponse, ReminderResponse, ResourceState, RevisionResponse } from '../shared/types'

function resource<T>(): ResourceState<T> {
  return { data: null, error: null, updatedAt: null, loading: false }
}

function newResources(): AdminResources {
  return {
    config: resource<ConfigResponse>(),
    reminders: resource<ReminderResponse>(),
    revisions: resource<RevisionResponse>(),
    incidents: resource<IncidentResponse>(),
    newsCatalog: resource<NewsCatalogResponse>(),
    prompts: resource<PromptResponse>(),
  }
}


const resourceNames: AdminResourceName[] = ['config', 'reminders', 'revisions', 'incidents', 'newsCatalog', 'prompts']

type ResourceResult<T> = { ok: true; data: T; updatedAt: number } | { ok: false; error: string; unauthorized: boolean }

async function capture<T>(load: () => Promise<T>): Promise<ResourceResult<T>> {
  try { return { ok: true, data: await load(), updatedAt: Date.now() } }
  catch (error) { return { ok: false, error: error instanceof Error ? error.message : '无法读取数据', unauthorized: error instanceof ApiError && error.status === 401 } }
}

function mergeResource<T>(current: ResourceState<T>, result: ResourceResult<T>): ResourceState<T> {
  return result.ok ? { data: result.data, error: null, updatedAt: result.updatedAt, loading: false } : { ...current, error: result.error, loading: false }
}

export function useAdminData(token: string, onUnauthorized: () => void) {
  const api = useMemo(() => new AdminApi(token), [token])
  const [resources, setResources] = useState<AdminResources>(newResources)
  const [refreshing, setRefreshing] = useState(false)
  const refreshSequence = useRef(0)


  const refresh = useCallback(async (quiet = false): Promise<boolean> => {
    if (!token) return false
    const sequence = ++refreshSequence.current
    if (!quiet) setRefreshing(true)
    setResources((current) => ({
      config: { ...current.config, loading: true },
      reminders: { ...current.reminders, loading: true },
      revisions: { ...current.revisions, loading: true },
      incidents: { ...current.incidents, loading: true },
      newsCatalog: { ...current.newsCatalog, loading: true },
      prompts: { ...current.prompts, loading: true },
    }))
    const [configResult, reminderResult, revisionResult, incidentResult, newsCatalogResult, promptsResult] = await Promise.all([
      capture(() => api.config()),
      capture(() => api.reminders()),
      capture(() => api.revisions()),
      capture(() => api.incidents()),
      capture(() => api.newsCatalog()),
      capture(() => api.prompts()),
    ])
    const results = [configResult, reminderResult, revisionResult, incidentResult, newsCatalogResult, promptsResult]
    if (sequence !== refreshSequence.current) return false
    if (results.some((result) => !result.ok && result.unauthorized)) {
      setResources(newResources())
      setRefreshing(false)
      onUnauthorized()
      return false
    }
    setResources((current) => ({
      config: mergeResource(current.config, configResult),
      reminders: mergeResource(current.reminders, reminderResult),
      revisions: mergeResource(current.revisions, revisionResult),
      incidents: mergeResource(current.incidents, incidentResult),
      newsCatalog: mergeResource(current.newsCatalog, newsCatalogResult),
      prompts: mergeResource(current.prompts, promptsResult),
    }))
    setRefreshing(false)
    return results.some((result) => result.ok)
  }, [api, onUnauthorized, token])

  useEffect(() => {
    if (!token) return
    const initial = window.setTimeout(() => { void refresh(true) }, 0)
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh(true)
    }, 30_000)
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refresh(true)
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      refreshSequence.current += 1
      window.clearTimeout(initial)
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [refresh, token])

  const reset = useCallback(() => {
    refreshSequence.current += 1
    setRefreshing(false)
    setResources(newResources())
  }, [])
  const lastUpdatedAt = Math.max(0, ...resourceNames.map((name) => resources[name].updatedAt || 0)) || null
  const hasAnyData = resourceNames.some((name) => resources[name].data !== null)

  return { api, resources, refresh, refreshing, reset, lastUpdatedAt, hasAnyData }
}
