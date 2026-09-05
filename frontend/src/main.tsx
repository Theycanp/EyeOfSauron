import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import AdminApp from './app/AdminApp'
import './styles.css'

const root = document.getElementById('app')
if (!root) throw new Error('Missing #app root element')

createRoot(root).render(<StrictMode><AdminApp /></StrictMode>)
