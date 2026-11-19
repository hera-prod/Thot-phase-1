import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SearchInput } from '@/components/ui/SearchInput';

describe('SearchInput', () => {
  it('renders with default placeholder', () => {
    render(<SearchInput />);
    expect(screen.getByPlaceholderText('Rechercher…')).toBeInTheDocument();
  });

  it('renders with custom placeholder', () => {
    render(<SearchInput placeholder="Chercher..." />);
    expect(screen.getByPlaceholderText('Chercher...')).toBeInTheDocument();
  });

  it('calls onChange when typing', () => {
    const handleChange = vi.fn();
    render(<SearchInput value="test" onChange={handleChange} />);
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'a' } });
    expect(handleChange).toHaveBeenCalled();
  });

  it('has search icon', () => {
    render(<SearchInput />);
    expect(screen.getByLabelText('Rechercher…')).toBeInTheDocument();
  });

  it('renders with value', () => {
    render(<SearchInput value="hello" />);
    expect(screen.getByDisplayValue('hello')).toBeInTheDocument();
  });
});