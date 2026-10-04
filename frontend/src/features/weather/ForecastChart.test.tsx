import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { WeatherHour } from '../../shared/types'
import { ForecastChart } from './ForecastChart'

const start = 1790805600
const hours: WeatherHour[] = [
  { at: start, temperature: -2, precipitation: 0, rain_probability: 0, precipitation_type: 'none' },
  { at: start + 3600, temperature: 0, precipitation: 1.2, rain_probability: 80, precipitation_type: 'snow' },
  { at: start + 7200, temperature: 2, precipitation: 0, rain_probability: 10, precipitation_type: 'none' },
]
const props = { timezone: 'Asia/Shanghai', high: 2, low: -2, start, end: start + 86400, observedAt: start, complete: false }

describe('ForecastChart', () => {
  it('lets the reader inspect a snow hour without mixing temperature and rain scales', () => {
    render(<ForecastChart {...props} hours={hours} />)
    expect(screen.getByText('气温 · °C')).toBeVisible()
    expect(screen.getByText('雨雪 · mm/h')).toBeVisible()
    fireEvent.change(screen.getByRole('slider', { name: '选择预报小时' }), { target: { value: '1' } })
    const detail = screen.getByLabelText('选中小时天气')
    expect(detail).toHaveTextContent('07:00')
    expect(detail).toHaveTextContent('0°C')
    expect(detail).toHaveTextContent('雪 1.2 mm/h')
    expect(detail).toHaveTextContent('降水概率 80%')
  })

  it('keeps missing precipitation distinct from zero and breaks the line at missing hours', () => {
    render(<ForecastChart {...props} hours={[
      hours[0]!, { at: start + 7200, temperature: 1, precipitation: null, rain_probability: null },
    ]} />)
    fireEvent.change(screen.getByRole('slider'), { target: { value: '1' } })
    expect(screen.getByLabelText('选中小时天气')).toHaveTextContent('降水量待获取')
    expect(screen.getByLabelText('选中小时天气')).toHaveTextContent('概率待获取')
    expect(screen.getByRole('img').querySelectorAll('.temperature-line')).toHaveLength(2)
  })

  it('reselects a valid hour when a refreshed forecast replaces the selected data', () => {
    const view = render(<ForecastChart {...props} hours={hours} />)
    fireEvent.change(screen.getByRole('slider'), { target: { value: '2' } })
    view.rerender(<ForecastChart {...props} hours={[hours[0]!]} />)
    expect(screen.getByRole('slider')).toHaveValue('0')
    expect(screen.getByLabelText('选中小时天气')).toHaveTextContent('06:00')
  })

  it('does not draw hours outside the saved window or fabricate an empty forecast', () => {
    render(<ForecastChart {...props} hours={[{ ...hours[0]!, at: start - 3600 }]} />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/暂无逐小时预报/)).toBeVisible()
  })
})
