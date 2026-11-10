import { apiFetch } from './http'

/**
 * 앱 데이터 완전 삭제 (설치형 프로그램의 '제거'처럼).
 * 서버가 삭제 도우미를 띄우고 앱을 종료한다 — 도우미가 앱이 끝나길 기다렸다가 앱 폴더를 지운다.
 */

export interface StorageItem {
  /** app: 앱 폴더 / temp: 지난 실행의 압축 해제 폴더 / shared: 라이브러리 공용 캐시(내용 있음) / empty: 빈 공용 캐시 */
  kind: 'app' | 'temp' | 'shared' | 'empty'
  label: string
  path: string
  bytes: number
}

export interface StoragePlan {
  frozen: boolean
  home: string
  /** exe 파일 위치 (개발 실행이면 null) */
  exePath: string | null
  items: StorageItem[]
  shared: StorageItem[]
  totalBytes: number
}

export async function getStoragePlan(): Promise<StoragePlan> {
  return apiFetch<StoragePlan>('/chatbot/app/storage')
}

export async function uninstallApp(opts: { confirm: string; includeShared: boolean; deleteExe: boolean }): Promise<{ targets: string[] }> {
  return apiFetch('/chatbot/app/uninstall', { method: 'POST', body: JSON.stringify(opts) })
}
