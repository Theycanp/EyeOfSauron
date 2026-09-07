import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { AdminApi } from '../../shared/api'
import { UsersPage } from './UsersPage'

describe('UsersPage', () => {
  it('shows session and audit state and creates a role-scoped user', async () => {
    const createUser = vi.fn().mockResolvedValue({ user: { id: 2 } })
    const api = {
      users: vi.fn().mockResolvedValue({ users: [{
        id: 1,
        username: 'owner',
        display_name: 'Owner',
        role: 'admin',
        enabled: true,
        created_at: 1,
        updated_at: 1,
        last_login_at: 1,
        active_sessions: 2,
      }] }),
      authAudit: vi.fn().mockResolvedValue({ audit: [{
        id: 1,
        user_id: 1,
        username: 'owner',
        action: 'login',
        actor: 'self',
        details: {},
        created_at: 1,
      }] }),
      createUser,
      updateUser: vi.fn(),
      resetUserPassword: vi.fn(),
      revokeUserSessions: vi.fn(),
    } as unknown as AdminApi
    const notify = vi.fn()
    render(<UsersPage api={api} currentUserId={1} notify={notify} onUnauthorized={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/2 个有效会话/)).toBeInTheDocument())
    expect(screen.getByText('登录成功')).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('用户名'), 'reader')
    await userEvent.type(screen.getByLabelText('显示名称'), 'Reader')
    await userEvent.type(screen.getByLabelText('初始密码'), 'reader-password-value')
    await userEvent.selectOptions(screen.getByLabelText('角色'), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: /创建用户/ }))

    await waitFor(() => expect(createUser).toHaveBeenCalledWith({
      username: 'reader',
      display_name: 'Reader',
      password: 'reader-password-value',
      role: 'viewer',
    }))
    expect(notify).toHaveBeenCalledWith('用户已创建')
  })
})
