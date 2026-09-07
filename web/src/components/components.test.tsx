import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DataTable, type Column } from '@/components/DataTable'
import { Field, Form, SubmitButton, TextInput } from '@/components/Form'
import { errorsFrom } from '@/components/formErrors'
import { StatusBadge } from '@/components/StatusBadge'
import { toneFor } from '@/components/status'

describe('<ConfirmDialog>', () => {
  const props = {
    open: true,
    title: 'Delete gateway',
    description: 'This cannot be undone.',
    resourceName: 'production-gateway',
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  }

  it('keeps the destructive action disabled until the name is typed', async () => {
    const user = userEvent.setup()
    render(<ConfirmDialog {...props} onConfirm={vi.fn()} />)

    const confirm = screen.getByRole('button', { name: 'Delete' })
    expect(confirm).toBeDisabled()

    await user.type(screen.getByRole('textbox'), 'production-gateway')

    expect(confirm).toBeEnabled()
  })

  it('is not satisfied by a near miss', async () => {
    const user = userEvent.setup()
    render(<ConfirmDialog {...props} />)

    await user.type(screen.getByRole('textbox'), 'production-gatewa')

    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
  })

  it('is case sensitive', async () => {
    // Normalising case would hand back the reflex the typed name exists to prevent.
    const user = userEvent.setup()
    render(<ConfirmDialog {...props} />)

    await user.type(screen.getByRole('textbox'), 'PRODUCTION-GATEWAY')

    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
  })

  it('confirms once the name matches', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    render(<ConfirmDialog {...props} onConfirm={onConfirm} />)

    await user.type(screen.getByRole('textbox'), 'production-gateway')
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('renders nothing when closed', () => {
    render(<ConfirmDialog {...props} open={false} />)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('<DataTable>', () => {
  type Row = { id: string; name: string; requests: number }
  const rows: Row[] = [
    { id: '1', name: 'beta', requests: 30 },
    { id: '2', name: 'alpha', requests: 10 },
    { id: '3', name: 'gamma', requests: 20 },
  ]
  const columns: Column<Row>[] = [
    { key: 'name', header: 'Name', render: (row) => row.name, sortValue: (row) => row.name },
    {
      key: 'requests',
      header: 'Requests',
      render: (row) => row.requests,
      sortValue: (row) => row.requests,
      align: 'right',
    },
  ]

  const names = () =>
    screen
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.querySelectorAll('td')[0]?.textContent)

  it('renders the rows it was given, in order', () => {
    render(<DataTable rows={rows} columns={columns} rowKey={(row) => row.id} />)

    expect(names()).toEqual(['beta', 'alpha', 'gamma'])
  })

  it('sorts locally when no handler is supplied', async () => {
    const user = userEvent.setup()
    render(<DataTable rows={rows} columns={columns} rowKey={(row) => row.id} />)

    await user.click(screen.getByRole('button', { name: /Name/ }))

    expect(names()).toEqual(['alpha', 'beta', 'gamma'])
  })

  it('reverses on a second click', async () => {
    const user = userEvent.setup()
    render(<DataTable rows={rows} columns={columns} rowKey={(row) => row.id} />)

    await user.click(screen.getByRole('button', { name: /Name/ }))
    await user.click(screen.getByRole('button', { name: /Name/ }))

    expect(names()).toEqual(['gamma', 'beta', 'alpha'])
  })

  it('delegates sorting to the server when asked to', async () => {
    // Sorting one page locally would be a lie about the other pages.
    const user = userEvent.setup()
    const onSortChange = vi.fn()
    render(
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        onSortChange={onSortChange}
      />,
    )

    await user.click(screen.getByRole('button', { name: /Requests/ }))

    expect(onSortChange).toHaveBeenCalledWith({ key: 'requests', direction: 'asc' })
    expect(names()).toEqual(['beta', 'alpha', 'gamma'])
  })

  it('announces the sort to assistive technology', async () => {
    const user = userEvent.setup()
    render(<DataTable rows={rows} columns={columns} rowKey={(row) => row.id} />)

    await user.click(screen.getByRole('button', { name: /Name/ }))

    expect(screen.getByRole('columnheader', { name: /Name/ })).toHaveAttribute(
      'aria-sort',
      'ascending',
    )
  })

  it('shows an instructional empty state instead of an empty grid', () => {
    render(
      <DataTable
        rows={[]}
        columns={columns}
        rowKey={(row) => row.id}
        emptyTitle="No gateways yet"
        emptyDescription="Create one to get an OpenAI-compatible endpoint."
      />,
    )

    expect(screen.getByText('No gateways yet')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('offers only the cursor directions that exist', () => {
    render(
      <DataTable
        rows={rows}
        columns={columns}
        rowKey={(row) => row.id}
        onNextPage={() => {}}
        onPreviousPage={null}
      />,
    )

    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
  })
})

describe('Form', () => {
  it('puts an API field error next to its field', () => {
    const error = new ApiError(422, 'validation_error', 'Request validation failed', {
      errors: [{ loc: ['body', 'email'], msg: 'not a valid email address' }],
    })

    render(
      <Form onSubmit={() => {}} error={error}>
        <Field name="email" label="Email">
          {(props) => <TextInput {...props} />}
        </Field>
        <SubmitButton>Save</SubmitButton>
      </Form>,
    )

    expect(screen.getByText('not a valid email address')).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toHaveAttribute('aria-invalid', 'true')
  })

  it('shows a whole-form error when no field is named', () => {
    const error = new ApiError(409, 'conflict', 'That slug is already taken.')

    render(
      <Form onSubmit={() => {}} error={error}>
        <SubmitButton>Save</SubmitButton>
      </Form>,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('That slug is already taken.')
  })

  it('does not repeat a message that is already shown per field', () => {
    const error = new ApiError(422, 'validation_error', 'Request validation failed', {
      errors: [{ loc: ['body', 'email'], msg: 'not a valid email address' }],
    })

    expect(errorsFrom(error).formError).toBeNull()
  })

  it('reports an unrecognised failure without leaking internals', () => {
    expect(errorsFrom(new TypeError('fetch failed')).formError).toBe(
      'Something went wrong. Try again.',
    )
  })

  it('submits without navigating', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()

    render(
      <Form onSubmit={onSubmit}>
        <SubmitButton>Save</SubmitButton>
      </Form>,
    )
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
  })
})

describe('<StatusBadge>', () => {
  it.each([
    ['active', 'ok'],
    ['failed', 'error'],
    ['suspended', 'warn'],
    ['ACTIVE', 'ok'],
  ])('maps %s to the %s tone', (status, tone) => {
    expect(toneFor(status)).toBe(tone)
  })

  it('falls back to neutral for a status it has never seen', () => {
    // The server owns this enum; a new value should not break the page.
    expect(toneFor('quiescent')).toBe('neutral')
  })

  it('shows the status text', () => {
    render(<StatusBadge status="active" />)

    expect(screen.getByText('active')).toBeInTheDocument()
  })
})
